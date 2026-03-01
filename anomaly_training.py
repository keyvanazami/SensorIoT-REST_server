"""anomaly_training.py — server-side ML anomaly detection training and prediction.

Uses the madi detector wrappers (IsolationForest, OneClassSVM, NS-RandomForest) and
utilities from the shared anomalydetection/ module. TensorFlow is NOT required on the
server; the deferred TF import in sample_utils only fires if you call
write_normalization_info / read_normalization_info, which we do not.

Trains one gateway-level model on a flattened multi-node DataFrame where each node's
readings appear as prefixed columns (e.g. 1_F, 1_H, 2_F, 2_H). This enables relative
anomaly detection across nodes. Saves the model with joblib and provides prediction
via the unified madi predict() interface.
"""

import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Locate the shared anomalydetection/ package regardless of working directory.
# Locally:  ./anomalydetection  (relative to this file in SensorIoT-REST_server/)
# Docker:   /anomalydetection    (COPY anomalydetection/ /anomalydetection/ in Dockerfile)
# ---------------------------------------------------------------------------
_madi_parent = os.path.abspath(
    os.path.join(os.path.dirname(__file__), 'anomalydetection'))
if _madi_parent not in sys.path:
    sys.path.insert(0, _madi_parent)

# Purge any previously-imported (e.g. pip-installed) madi modules so the
# local copy from _madi_parent is used instead.
for _key in [k for k in sys.modules if k == 'madi' or k.startswith('madi.')]:
    del sys.modules[_key]

from madi.detectors.isolation_forest_detector import IsolationForestAd
from madi.detectors.one_class_svm import OneClassSVMAd
from madi.detectors.neg_sample_random_forest import NegativeSamplingRandomForestAd
import madi.utils.sample_utils as sample_utils
from madi.utils.evaluation_utils import compute_auc

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_log_fmt = logging.Formatter(
    '%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

# Log to stdout
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setLevel(logging.DEBUG)
_stdout_handler.setFormatter(_log_fmt)
logger.addHandler(_stdout_handler)

# Log to file (in the same directory as this module)
_log_file = os.path.join(os.path.dirname(__file__), 'anomaly_training.log')
_file_handler = logging.FileHandler(_log_file)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_log_fmt)
logger.addHandler(_file_handler)

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
_LOOKBACK_DAYS     = 90
_SAMPLE_RATIO      = 2.0
_SAMPLE_DELTA      = 0.05
_TEST_RATIO        = 1.0
_ANOMALY_THRESHOLD = 0.5
_BUCKET_SECONDS    = 60   # fallback / minimum bucket size
# Candidate bucket sizes tried in order (seconds); first one >= max node interval is used.
_BUCKET_CANDIDATES = (60, 120, 300, 600, 900, 1800, 3600)


def _optimal_bucket_seconds(df: pd.DataFrame, node_ids: List[str]) -> int:
    """Return the smallest snap interval that covers every node's median reporting gap.

    Nodes send F/H/P in one shot, so aligning across nodes only requires a bucket
    large enough that all nodes fire at least once per window.  We compute the
    median inter-reading gap for each node and pick the first _BUCKET_CANDIDATES
    value that is >= the slowest node's median interval.
    """
    intervals = []
    for n in node_ids:
        times = np.sort(df[(df['node_id'] == n)  & (df['type'] == 'F')]['time'].values)
        if len(times) >= 2:
            intervals.append(float(np.median(np.diff(times))))

    if not intervals:
        return _BUCKET_SECONDS

    max_interval = max(intervals)
    logger.debug('Node intervals: %s, max=%.1f s', ', '.join(f'{i:.1f}' for i in intervals), max_interval)
    for snap in _BUCKET_CANDIDATES:
        if snap >= max_interval:
            return snap
    return _BUCKET_CANDIDATES[-1]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def get_gateway_dataframe(db, gateway_id: str,
                          lookback_days: int = _LOOKBACK_DAYS) -> Optional[pd.DataFrame]:
    """Query all nodes for gateway_id; return a wide time-bucketed DataFrame.

    Columns are '{node_id}_{type}' (e.g. '1_F', '1_H', '2_F', '2_P').
    The bucket size is chosen dynamically: for each node the median inter-reading
    interval is computed; the bucket is set to the smallest value in
    _BUCKET_CANDIDATES that is >= the slowest node's interval. This ensures all
    nodes have at least one reading per bucket regardless of their duty cycle.
    Rows missing F or H for any node are dropped; P columns are allowed to be
    sparse. The returned DataFrame includes a 'time_rounded' column.
    Returns None if fewer than 20 aligned rows remain.
    """
    start_ts = time.time() - lookback_days * 86400
    try:
        cursor = db.Sensors.find(
            {'gateway_id': gateway_id,
             'time': {'$gte': start_ts},
             'type': {'$in': ['F', 'H', 'P']}},
            {'_id': 0, 'node_id': 1, 'type': 1, 'value': 1, 'time': 1},
        )
        rows = list(cursor)
    except Exception as exc:
        logger.warning('MongoDB query failed for gateway %s: %s', gateway_id, exc)
        return None

    if not rows:
        logger.info('No sensor rows found for gateway %s in last %d days', gateway_id, lookback_days)
        return None

    df = pd.DataFrame(rows)

    def _clean(v):
        try:
            return float(str(v).replace("b'", '').replace("'", ''))
        except (ValueError, TypeError):
            return float('nan')

    df['value'] = df['value'].apply(_clean)
    df = df.dropna(subset=['value'])
    df['node_id'] = df['node_id'].astype(str)

    node_ids = sorted(df['node_id'].unique())
    bucket_secs = _optimal_bucket_seconds(df, node_ids)
    logger.info('Gateway %s: using %d s buckets (nodes=%s)', gateway_id, bucket_secs, node_ids)

    df['bucket'] = (df['time'] // bucket_secs).astype(int) * bucket_secs
    df['col'] = df['node_id'] + '_' + df['type']

    pivoted = df.pivot_table(
        index='bucket', columns='col', values='value', aggfunc='first'
    )
    pivoted.columns.name = None

    # Require F and H for every node that contributed data in this window
    required = [f'{n}_{t}' for n in node_ids for t in ('F', 'H')
                if f'{n}_{t}' in pivoted.columns]
    if not required:
        logger.info('No usable F/H columns for gateway %s', gateway_id)
        return None

    result = pivoted.dropna(subset=required).reset_index(drop=False)
    result = result.rename(columns={'bucket': 'time_rounded'})

    if len(result) < 20:
        logger.info('Gateway %s: only %d aligned rows after dropna', gateway_id, len(result))
        return None

    feature_cols = [c for c in result.columns if c != 'time_rounded']
    logger.info('Gateway %s: %d aligned rows, %d feature columns: %s',
                gateway_id, len(result), len(feature_cols), feature_cols)
    return result


# ---------------------------------------------------------------------------
# Model training & selection
# ---------------------------------------------------------------------------

def train_and_select_best(
    node_df: pd.DataFrame,
    random_state: int = 42,
) -> Tuple[object, str, float, List[str]]:
    """Train IF / OC-SVM / NS-RF via madi wrappers, pick best by AUC.

    Returns (best_detector, model_type_name, auc, feature_columns).
    feature_columns is derived from the input DataFrame columns — for gateway-level
    training these are prefixed (e.g. '1_F', '1_H', '2_F'). Normalization is embedded
    in each detector's _normalization_info attribute and persisted automatically by
    joblib when save_model() is called.
    """
    np.random.seed(random_state)

    feature_cols = node_df.columns.tolist()

    n_train = int(0.8 * len(node_df))
    shuffled = node_df.sample(frac=1, random_state=random_state).reset_index(drop=True)
    x_train    = shuffled.iloc[:n_train][feature_cols]
    x_test_raw = shuffled.iloc[n_train:][feature_cols]

    # Synthetic test set: real positives + permuted negatives
    pos_test = x_test_raw.copy()
    pos_test['class_label'] = 1
    neg_test = sample_utils.get_neg_sample(
        x_test_raw, int(len(x_test_raw) * _TEST_RATIO), do_permute=True)
    test_combined = pd.concat([pos_test, neg_test], ignore_index=True).sample(frac=1)
    X_test_df = test_combined[feature_cols]
    y_test    = test_combined['class_label'].values

    detectors = {
        'IsolationForest': IsolationForestAd(contamination=0.05, random_state=random_state),
        'OneClassSVM':     OneClassSVMAd(nu=0.1),
        'NS-RandomForest': NegativeSamplingRandomForestAd(
            n_estimators=100, random_state=random_state,
            sample_ratio=_SAMPLE_RATIO, sample_delta=_SAMPLE_DELTA),
    }

    results = {}
    for name, det in detectors.items():
        try:
            logger.debug('Training %s...', name)
            det.train_model(x_train)
            pred_df = det.predict(X_test_df.copy())
            auc = float(compute_auc(y_test, pred_df['class_prob'].values))
            results[name] = (det, auc)
            logger.info('%s AUC=%.4f', name, auc)
        except Exception as exc:
            logger.warning('%s failed: %s', name, exc)

    if not results:
        raise RuntimeError('All detectors failed to train')

    best_name = max(results, key=lambda k: results[k][1])
    best_det, best_auc = results[best_name]
    logger.info('Best model: %s (AUC=%.4f) features=%s', best_name, best_auc, feature_cols)
    return best_det, best_name, best_auc, feature_cols


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def _model_dir(gateway_id: str, models_dir: str = MODELS_DIR) -> str:
    return os.path.join(models_dir, str(gateway_id))


def save_model(gateway_id: str, model, model_type: str,
               auc: float, feature_columns: List[str], nodes: List[str],
               num_rows: int,
               models_dir: str = MODELS_DIR) -> None:
    """Persist gateway-level model + metadata to models/{gateway}/.

    Normalization is embedded inside the detector object and saved by joblib;
    no separate normalization.json is needed.
    """
    path = _model_dir(gateway_id, models_dir)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.error('Failed to create model directory %s: %s', path, exc)
        raise

    model_path = os.path.join(path, 'model.joblib')
    meta_path = os.path.join(path, 'metadata.json')

    try:
        joblib.dump(model, model_path)
    except Exception as exc:
        logger.error('Failed to save model to %s: %s', model_path, exc)
        raise

    try:
        with open(meta_path, 'w') as f:
            json.dump({'model_type': model_type, 'auc': auc,
                       'feature_columns': feature_columns,
                       'nodes': nodes,
                       'num_rows': num_rows,
                       'trained_at': time.time()}, f)
    except Exception as exc:
        logger.error('Failed to save metadata to %s: %s', meta_path, exc)
        if os.path.isfile(model_path):
            os.remove(model_path)
        raise

    logger.info('Saved %s gateway model (AUC=%.4f) nodes=%s num_rows=%d to %s',
                model_type, auc, nodes, num_rows, path)


def load_model(gateway_id: str, models_dir: str = MODELS_DIR) -> Tuple[object, Dict]:
    """Load (model, metadata). Raises FileNotFoundError if absent."""
    path = _model_dir(gateway_id, models_dir)
    model = joblib.load(os.path.join(path, 'model.joblib'))
    with open(os.path.join(path, 'metadata.json')) as f:
        metadata = json.load(f)
    return model, metadata


def model_exists(gateway_id: str, models_dir: str = MODELS_DIR) -> bool:
    return os.path.isfile(os.path.join(_model_dir(gateway_id, models_dir), 'model.joblib'))


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

def predict_anomalies(model, fh_df: pd.DataFrame,
                      threshold: float = _ANOMALY_THRESHOLD,
                      feature_columns: Optional[List[str]] = None) -> List[float]:
    """Return Unix timestamps where class_prob < threshold (anomalous).

    fh_df must contain the feature columns used during training (at minimum F and H)
    and either a numeric index of Unix timestamps or a 'time_rounded' column.
    feature_columns defaults to ['F', 'H'] for backwards compatibility with models
    trained before P support was added. class_prob: 1.0 = normal, 0.0 = anomalous.
    """
    
    logger.debug('Predicting anomalies for %s with threshold %.2f', fh_df.shape, threshold)
    logger.debug('Feature columns: %s', feature_columns if feature_columns else ['F', 'H', 'P'])
    logger.debug('Input columns: %s', fh_df.columns.tolist())
    logger.debug('Input head:\n%s', fh_df.head())

    if fh_df.empty:
        return []
    

    logger.debug('Predicting anomalies for %d rows with features %s',
                 len(fh_df), feature_columns if feature_columns else ['F', 'H', 'P'])
    cols = feature_columns if feature_columns else ['F', 'H', 'P']
    # Only use columns present in the dataframe (P may be absent for some nodes)
    cols = [c for c in cols if c in fh_df.columns]

    timestamps = (fh_df['time_rounded'].values
                  if 'time_rounded' in fh_df.columns
                  else fh_df.index.values.astype(float))

    try:
        result = model.predict(fh_df[cols].copy())
        mask = result['class_prob'].values < threshold
    except Exception as exc:
        logger.warning('Prediction failed: %s', exc)
        return []

    anomalies = [float(ts) for ts, m in zip(timestamps, mask) if m]

    logger.info('Predicted %d anomalies out of %d rows (%.2f%%) with threshold %.2f',
                len(anomalies), len(fh_df), 100 * len(anomalies) / len(fh_df), threshold)

    return anomalies


# ---------------------------------------------------------------------------
# Gateway-level orchestration (called from server.py background thread)
# ---------------------------------------------------------------------------

def train_for_gateway(gateway_id: str, db,
                      models_dir: str = MODELS_DIR) -> List[Dict]:
    """Train one gateway-level model on flattened multi-node data.

    All nodes' F/H/P readings are combined into a single wide DataFrame with
    columns prefixed by node_id (e.g. '1_F', '1_H', '2_F'). One model is trained
    per gateway and saved to models/{gateway_id}/.
    """
    logger.info('Building gateway-wide DataFrame for %s', gateway_id)
    gw_df = get_gateway_dataframe(db, gateway_id)
    if gw_df is None:
        logger.info('Skipping gateway %s: insufficient aligned data', gateway_id)
        return [{'gateway_id': gateway_id, 'status': 'skipped',
                 'reason': 'insufficient aligned data'}]

    feature_df = gw_df.drop(columns=['time_rounded'])
    try:
        model, model_type, auc, feature_cols = train_and_select_best(feature_df)
    except Exception as exc:
        logger.error('Training failed for gateway %s: %s', gateway_id, exc)
        return [{'gateway_id': gateway_id, 'status': 'failed', 'error': str(exc)}]

    nodes = sorted({c.rsplit('_', 1)[0] for c in feature_cols})
    num_rows = len(feature_df)
    save_model(gateway_id, model, model_type, auc, feature_cols, nodes, num_rows, models_dir)
    logger.info('Training complete for gateway %s: %s AUC=%.4f nodes=%s num_rows=%d',
                gateway_id, model_type, auc, nodes, num_rows)
    return [{'gateway_id': gateway_id, 'status': 'done',
             'model_type': model_type, 'auc': round(auc, 4),
             'feature_columns': feature_cols, 'nodes': nodes, 'num_rows': num_rows}]
