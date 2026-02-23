"""Tests for the Google Home fulfillment blueprint."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

import app_state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _issue_token(client) -> str:
    """Create an OAuth code, exchange it for an access token, return the token."""
    resp = client.post('/auth', data={
        'client_id': 'test-client',
        'redirect_uri': 'https://example.com/cb',
        'state': 's',
    })
    code = parse_qs(urlparse(resp.headers['Location']).query)['code'][0]
    token_resp = client.post('/token', data={'grant_type': 'authorization_code', 'code': code})
    return json.loads(token_resp.data)['access_token']


def _fulfillment(client, intent, token, extra_payload=None):
    payload = {
        'requestId': 'test-req-1',
        'inputs': [{'intent': intent, 'payload': extra_payload or {}}],
    }
    return client.post(
        '/fulfillment',
        data=json.dumps(payload),
        content_type='application/json',
        headers={'Authorization': f'Bearer {token}'},
    )


# ── Health check ──────────────────────────────────────────────────────────────

class TestFulfillmentHealth:
    def test_health_check_returns_200(self, client):
        resp = client.get('/fulfillment/test')
        assert resp.status_code == 200
        assert json.loads(resp.data)['status'] == 'ok'


# ── Authorization guard ───────────────────────────────────────────────────────

class TestFulfillmentAuth:
    def _sync_body(self):
        return json.dumps({
            'requestId': 'r',
            'inputs': [{'intent': 'action.devices.SYNC'}],
        })

    def test_no_auth_header_returns_401(self, client):
        resp = client.post('/fulfillment', data=self._sync_body(), content_type='application/json')
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self, client):
        resp = client.post(
            '/fulfillment',
            data=self._sync_body(),
            content_type='application/json',
            headers={'Authorization': 'Bearer bad-token-xyz'},
        )
        assert resp.status_code == 401

    def test_malformed_auth_scheme_returns_401(self, client):
        resp = client.post(
            '/fulfillment',
            data=self._sync_body(),
            content_type='application/json',
            headers={'Authorization': 'NotBearer sometoken'},
        )
        assert resp.status_code == 401


# ── SYNC ──────────────────────────────────────────────────────────────────────

class TestSync:
    def test_sync_returns_200(self, client):
        token = _issue_token(client)
        assert _fulfillment(client, 'action.devices.SYNC', token).status_code == 200

    def test_sync_echoes_request_id(self, client):
        token = _issue_token(client)
        data = json.loads(_fulfillment(client, 'action.devices.SYNC', token).data)
        assert data['requestId'] == 'test-req-1'

    def test_sync_returns_device_list(self, client):
        token = _issue_token(client)
        data = json.loads(_fulfillment(client, 'action.devices.SYNC', token).data)
        devices = data['payload']['devices']
        assert isinstance(devices, list)
        assert len(devices) >= 1

    def test_sync_includes_device_1(self, client):
        token = _issue_token(client)
        data = json.loads(_fulfillment(client, 'action.devices.SYNC', token).data)
        ids = [d['id'] for d in data['payload']['devices']]
        assert 'device_1' in ids

    def test_sync_device_has_required_fields(self, client):
        token = _issue_token(client)
        device = json.loads(_fulfillment(client, 'action.devices.SYNC', token).data)['payload']['devices'][0]
        for field in ('id', 'type', 'traits', 'name', 'willReportState'):
            assert field in device


# ── QUERY ─────────────────────────────────────────────────────────────────────

class TestQuery:
    def test_query_returns_device_state(self, client):
        token = _issue_token(client)
        data = json.loads(
            _fulfillment(client, 'action.devices.QUERY', token,
                         extra_payload={'devices': [{'id': 'device_1'}]}).data
        )
        assert 'device_1' in data['payload']['devices']
        assert 'on' in data['payload']['devices']['device_1']

    def test_query_unknown_device_returns_empty(self, client):
        token = _issue_token(client)
        data = json.loads(
            _fulfillment(client, 'action.devices.QUERY', token,
                         extra_payload={'devices': [{'id': 'unknown_device'}]}).data
        )
        assert data['payload']['devices'] == {}

    def test_query_initial_state_is_off(self, client):
        token = _issue_token(client)
        data = json.loads(
            _fulfillment(client, 'action.devices.QUERY', token,
                         extra_payload={'devices': [{'id': 'device_1'}]}).data
        )
        assert data['payload']['devices']['device_1']['on'] is False


# ── EXECUTE ───────────────────────────────────────────────────────────────────

def _execute_onoff(client, token, on: bool):
    payload = {
        'requestId': 'exec-req',
        'inputs': [{
            'intent': 'action.devices.EXECUTE',
            'payload': {
                'commands': [{
                    'devices': [{'id': 'device_1'}],
                    'execution': [{
                        'command': 'action.devices.commands.OnOff',
                        'params': {'on': on},
                    }],
                }],
            },
        }],
    }
    return client.post(
        '/fulfillment',
        data=json.dumps(payload),
        content_type='application/json',
        headers={'Authorization': f'Bearer {token}'},
    )


class TestExecute:
    def test_execute_on_sets_state_true(self, client):
        token = _issue_token(client)
        _execute_onoff(client, token, on=True)
        assert app_state.MOCK_DEVICES['device_1']['state']['on'] is True

    def test_execute_off_sets_state_false(self, client):
        app_state.MOCK_DEVICES['device_1']['state']['on'] = True
        token = _issue_token(client)
        _execute_onoff(client, token, on=False)
        assert app_state.MOCK_DEVICES['device_1']['state']['on'] is False

    def test_execute_returns_success_status(self, client):
        token = _issue_token(client)
        data = json.loads(_execute_onoff(client, token, on=True).data)
        assert data['payload']['commands'][0]['status'] == 'SUCCESS'

    def test_execute_response_echoes_device_id(self, client):
        token = _issue_token(client)
        data = json.loads(_execute_onoff(client, token, on=True).data)
        assert 'device_1' in data['payload']['commands'][0]['ids']
