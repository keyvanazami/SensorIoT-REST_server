from flask import Flask, request
from flask_cors import CORS
from dotenv import load_dotenv
from dateutil.tz import tzutc, gettz
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from pymongo import MongoClient
import pymongo
import json
import datetime as dt
import base64
load_dotenv()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

print('connecting to mongo...')
client = MongoClient('localhost', 27017)
db = client['gdtechdb_prod']
sensors = db['Sensors']
sensorsLatest = db['SensorsLatest']
nicknames = db['Nicknames']
userProfiles = db['UserProfiles']

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)

from auth import auth_bp
from fulfillment import fulfillment_bp
app.register_blueprint(auth_bp)
app.register_blueprint(fulfillment_bp)

timefmt = '%Y-%m-%d %H:%M:%S'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cleanvalue(value):
    return float(value.replace('b', '').replace('v', '').replace("'", ""))


def getstart(p):
    """Return a Unix timestamp p hours before now. Defaults to 24 h."""
    nowdatetime = dt.datetime.now(tzutc())
    if p is None:
        diff = dt.timedelta(hours=24)
    else:
        diff = dt.timedelta(hours=int(p))
    return (nowdatetime - diff).timestamp()


def decrypt_password_aes(encrypted_password_base64, shared_key_base64):
    """Decrypt a base64-encoded AES-256-CBC password. IV is prepended to ciphertext."""
    try:
        shared_key_bytes = base64.urlsafe_b64decode(shared_key_base64)
        encrypted_data = base64.urlsafe_b64decode(encrypted_password_base64)
        iv = encrypted_data[:16]
        ciphertext = encrypted_data[16:]
        cipher = Cipher(algorithms.AES(shared_key_bytes), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted_padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        decrypted_data = unpadder.update(decrypted_padded) + unpadder.finalize()
        return decrypted_data.decode('utf-8')
    except Exception as e:
        print(f"Decryption error: {e}")
        return None

# ---------------------------------------------------------------------------
# Sensor Data
# ---------------------------------------------------------------------------

@app.route("/", methods=['GET'])
def hello():
    return 'hello ' + request.args.get('name', '')


@app.route("/stats", methods=['GET'])
def stats():
    ct = sensors.countDocuments()
    return 'total rows:' + str(ct)


@app.route('/sensorlist', methods=['GET'])
def sensorlist():
    d = sensors.distinct('node_id')
    return json.dumps(d)


@app.route("/sensor/<node>", methods=['GET'])
def sensor(node):
    skip = request.args.get('skip', '')
    type = request.args.get('type', '')
    period = request.args.get('period')
    try:
        skip = int(skip)
    except ValueError:
        skip = 0
    try:
        period = int(period) * 24
    except (ValueError, TypeError):
        period = 24
    return json.dumps(getdata(node, getstart(period), skip, type))


@app.route("/latest/<gw>", methods=['GET'])
def latest(gw):
    try:
        period = int(request.args.get('period', ''))
    except ValueError:
        period = 24
    return json.dumps(getlatest(gw, getstart(period)))


@app.route("/latests", methods=['GET'])
def latests():
    gateways = request.args.getlist('gw')
    try:
        period = int(request.args.get('period', ''))
    except ValueError:
        period = 1
    start = getstart(period)
    results = [{'gateway_id': gw, 'latest': getlatest(gw, start)} for gw in gateways]
    return json.dumps(results)


@app.route("/nodelist/<gw>", methods=['GET'])
def nodelist(gw):
    try:
        period = int(request.args.get('period')) * 24
    except TypeError:
        period = 24
    return json.dumps(sorted(getnodelist(gw, getstart(period))))


@app.route("/nodelists", methods=['GET'])
def nodelists():
    gateways = request.args.getlist('gw')
    try:
        period = int(request.args.get('period')) * 24
    except TypeError:
        period = 24
    start = getstart(period)
    results = [{'gateway_id': gw, 'nodes': getnodelist(gw, start)} for gw in gateways]
    return json.dumps(results)


@app.route("/gw/<gw>", methods=['GET'])
def gw_data(gw):
    nodes = request.args.getlist('node')
    type = request.args.get('type', '')
    timezone = request.args.get('timezone', 'None')
    try:
        period = int(request.args.get('period')) * 24
    except (ValueError, TypeError):
        period = 24
    print('calling gwiteratenodes with', gw, nodes, type, period, timezone, 'at', dt.datetime.now())
    return json.dumps(gwiteratenodes(gw, nodes, type, period, timezone))


def getnodelist(gw, start):
    qry = {'gateway_id': gw, 'time': {'$gte': start}}
    print('query is %s' % qry)
    values = sensorsLatest.distinct('node_id', qry)
    print('query returned at', dt.datetime.now())
    return values


def getlatest(gw, start):
    docs = []
    qry = {'gateway_id': gw, 'time': {'$gte': int(start)}}
    sortparam = [('node_id', -1)]
    cursor = sensorsLatest.find(qry).sort(sortparam)
    for doc in cursor:
        docs.append({
            'node_id': doc['node_id'],
            'type': doc['type'],
            'gateway_id': gw,
            'value': cleanvalue(doc['value']),
            'time': doc['time'],
            'human_time': dt.datetime.fromtimestamp(doc['time']).strftime(timefmt),
        })
    return docs


def gwiteratenodes(gw, nodes, type, period, timezone):
    start = getstart(period)
    returndocs = []
    for node in nodes:
        print('calling getdatausinggw with', gw, node, start, type, timezone, dt.datetime.now())
        record = {
            'gateway_id': gw,
            'nodeID': node,
            'sensorData': getdatausinggw(gw, node, start, type, timezone),
        }
        returndocs.append(record)
    return returndocs


def getdatausinggw(gw, node, start, mytype, timezone):
    print('getdatausinggw starting. C extensions in use:', pymongo.has_c())
    docs = []
    empty_results = {'results': '0'}

    try:
        toZone = gettz(timezone)
        fromZone = tzutc()
    except ValueError:
        print('Invalid timezone parameter %s. Defaulting to 0' % timezone)
        toZone = gettz('UTC')
        fromZone = tzutc()

    qry = {'gateway_id': gw, 'node_id': str(node), 'time': {'$gte': start}}
    sortparam = [('time', 1)]
    if mytype:
        qry['type'] = mytype

    print('query is %s and sort is ' % qry, sortparam)
    print('starting query at', dt.datetime.now())
    resultsarray = list(sensors.find(qry).sort(sortparam).batch_size(100000))
    count = len(resultsarray)
    print('%i records returned' % count, dt.datetime.now())

    if count == 0:
        return empty_results

    ct = 0
    total = 0
    skip = 0
    if count > 300:
        skip = int(count / 300 + .49)
        print('Since more than 300 records were returned, skip is set to %i' % skip, dt.datetime.now())

    # Insert initial goalpost doc at start time
    newdoc = {'value': 0, 'human_time': '', 'time': 0}
    newdoc['human_time'] = dt.datetime.fromtimestamp(start).replace(tzinfo=fromZone).astimezone(toZone).strftime(timefmt)
    newdoc['time'] = start
    newdoc['value'] = cleanvalue(resultsarray[skip + 1]['value'])
    docs.append(newdoc)

    latestvalue = newdoc['value']
    for doc in resultsarray:
        total += 1
        ct += 1
        newdoc = {'value': 0, 'human_time': '', 'time': 0}
        if ct > skip:
            newvalue = cleanvalue(doc['value'])
            newdoc['value'] = newvalue
            latestvalue = newvalue
            newdoc['human_time'] = dt.datetime.fromtimestamp(doc['time']).replace(tzinfo=fromZone).astimezone(toZone).strftime(timefmt)
            newdoc['time'] = doc['time']
            docs.append(newdoc)
            ct = 0
    if ct != 0:
        newdoc['value'] = cleanvalue(doc['value'])
        newdoc['human_time'] = dt.datetime.fromtimestamp(doc['time']).replace(tzinfo=fromZone).astimezone(toZone).strftime(timefmt)
        newdoc['time'] = doc['time']
        docs.append(newdoc)

    # Insert final goalpost doc at current time
    now = dt.datetime.timestamp(dt.datetime.now())
    docs.append({'value': latestvalue, 'human_time': now, 'time': now})

    print('total docs found:', total, ' and returning:', len(docs))
    return docs


def getdata(node, start, skip, mytype):
    print('getdata starting...')
    docs = []
    qry = {'node_id': node, 'time': {'$gte': start}}
    sortparam = [('time', -1)]
    if mytype:
        qry['type'] = mytype
    print('query is %s and sort is ' % qry, sortparam)
    cursor = sensors.find(qry).sort(sortparam)
    ct = 0
    total = 0
    for doc in cursor:
        total += 1
        ct += 1
        if ct > skip:
            doc['_id'] = str(doc['_id'])
            doc['value'] = cleanvalue(doc['value'])
            doc['human_time'] = dt.datetime.fromtimestamp(doc['time']).strftime(timefmt)
            if 'iso_time' in doc:
                doc['iso_time'] = str(doc['iso_time'])
            docs.append(doc)
            ct = 0
    docs.insert(0, len(docs))
    print('total docs found:', total, ' and returning:', len(docs))
    return docs

# ---------------------------------------------------------------------------
# Nicknames
# ---------------------------------------------------------------------------

@app.route("/get_nicknames", methods=['GET'])
def get_nicknames():
    gateways = request.args.getlist('gw')
    returndoc = []
    for gateway in gateways:
        filt = {'gateway_id': gateway}
        node_rows = list(db.Nicknames.find(filt, {'_id': 0}).sort([('node_id', 1)]))
        nicknames_list = [
            {'node_id': r['node_id'], 'shortname': r['shortname'],
             'longname': r['longname'], 'seq_no': r['seq_no']}
            for r in node_rows
        ]
        gw_doc = db.GWNicknames.find_one(filt) or {}
        returndoc.append({
            'gateway_id': gateway,
            'longname': gw_doc.get('longname', ''),
            'seq_no': gw_doc.get('seq_no', 0),
            'nicknames': nicknames_list,
        })
    return json.dumps(returndoc)


@app.route("/save_nicknames", methods=['POST'])
def save_nicknames():
    for group in request.get_json():
        gw = group['gateway_id']
        gwLongname = group.get('longname', '')
        db.GWNicknames.update_one(
            {'gateway_id': gw},
            {'$set': {'gateway_id': gw, 'longname': gwLongname}, '$inc': {'seq_no': 1}},
            upsert=True)
        for item in group.get('nicknames', []):
            db.Nicknames.update_one(
                {'gateway_id': gw, 'node_id': item['nodeID']},
                {'$set': {
                    'gateway_id': gw, 'node_id': item['nodeID'],
                    'shortname': item['shortname'], 'longname': item['longname'],
                }, '$inc': {'seq_no': 1}},
                upsert=True)
    return 'OK'

# ---------------------------------------------------------------------------
# Third-Party Services (Sense Energy)
# ---------------------------------------------------------------------------

def _load_3p_services(logins):
    results = []
    for login in logins:
        for row in db.ThirdPartyServices.find({'login': login}).sort([('service_name', 1)]):
            results.append({
                'service_name': row['service_name'],
                'login': row['login'],
                'password': row['password'],
                'type': row['service_type'],
            })
    return results


@app.route("/add_3p_service", methods=['POST'])
def add_3p_service():
    conn_data = request.get_json()
    db.ThirdPartyServices.update_one(
        {'service_name': conn_data['service_name'], 'login': conn_data['login']},
        {'$set': {
            'service_name': conn_data['service_name'],
            'login': conn_data['login'],
            'password': conn_data['password'],
            'service_type': conn_data['service_type'],
        }},
        upsert=True)
    return 'OK'


@app.route("/get_3p_services", methods=['GET'])
def get_3p_services():
    logins = request.args.getlist('logins')
    return json.dumps(_load_3p_services(logins))


@app.route("/testsense", methods=['GET'])
def testsense():
    from sense_energy import Senseable
    login = request.args.get('login')
    key = request.args.get('key')
    svc = _load_3p_services([login])
    password = decrypt_password_aes(svc[0]['password'], key)
    sense = Senseable()
    sense.authenticate(login, password)
    sense.update_realtime()
    pwr = round(sense.active_power, 2)
    gw_name = '%s@%s' % (login, svc[0]['service_name'])
    now = dt.datetime.timestamp(dt.datetime.now())
    db.Sensors.insert_one({
        'model': svc[0]['type'], 'gateway_id': gw_name,
        'node_id': '0', 'type': 'PWR', 'value': str(pwr), 'time': now,
    })
    db.SensorsLatest.update_one(
        {'gateway_id': gw_name, 'node_id': '0', 'type': 'PWR'},
        {'$set': {
            'model': svc[0]['type'], 'gateway_id': gw_name,
            'node_id': '0', 'type': 'PWR', 'value': str(pwr), 'time': now,
        }},
        upsert=True)
    return json.dumps(pwr)

# ---------------------------------------------------------------------------
# User Profiles
# ---------------------------------------------------------------------------

@app.route("/user_profile", methods=['GET'])
def get_user_profile():
    print('DEBUG: get_user_profile called with args:', request.args)
    email = request.args.get('email', '')
    if not email:
        return json.dumps({}), 400
    doc = db.UserProfiles.find_one({'email': email}, {'_id': 0})
    if doc is None:
        return json.dumps({}), 404
    return json.dumps(doc)


@app.route("/user_profile", methods=['POST'])
def save_user_profile():
    print('DEBUG: save_user_profile called with JSON:', request.get_json())
    data = request.get_json()
    email = data.get('email', '')
    if not email:
        return 'missing email', 400
    db.UserProfiles.update_one(
        {'email': email},
        {'$set': {
            'email': email,
            'gateway_ids': data.get('gateway_ids', []),
            'updated_at': dt.datetime.now(dt.timezone.utc).timestamp(),
        }},
        upsert=True)
    return 'OK'

# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=True)
