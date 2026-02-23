import os
from flask import Blueprint, request, jsonify
from app_state import MOCK_DEVICES, OAUTH_TOKENS

fulfillment_bp = Blueprint('fulfillment', __name__)

def verify_token(auth_header):
    if not auth_header or not auth_header.startswith('Bearer '):
        return None
    token = auth_header.split(' ')[1]

    if token in OAUTH_TOKENS:
        return OAUTH_TOKENS[token]

    return None

@fulfillment_bp.route('/fulfillment/test', methods=['GET'])
def fulfillment_test():
    return jsonify({"status": "ok", "message": "Fulfillment route is accessible."})

@fulfillment_bp.route('/fulfillment', methods=['POST'])
def fulfillment():
    """
    The main webhook for Google Smart Home Action.
    Receives SYNC, QUERY, and EXECUTE intents.
    """
    # Validate the Authorization header Bearer token
    auth_header = request.headers.get('Authorization')
    user_info = verify_token(auth_header)

    if not user_info:
        return jsonify({"error": "invalid_grant"}), 401

    # Extract the user ID from our mock token
    user_id = user_info.get('user_id')

    body = request.get_json()
    intent = body['inputs'][0]['intent']
    request_id = body.get('requestId')

    print(f"Received Google Home intent: {intent} for Google User: {user_id}")

    if intent == 'action.devices.SYNC':
        return handle_sync(request_id, user_id)
    elif intent == 'action.devices.QUERY':
        return handle_query(request_id, body['inputs'][0]['payload'])
    elif intent == 'action.devices.EXECUTE':
        return handle_execute(request_id, body['inputs'][0]['payload'])

    return jsonify({"error": "unknown_intent"}), 400


def handle_sync(request_id, agent_user_id):
    devices = []
    for device_id, device_info in MOCK_DEVICES.items():
        devices.append({
            "id": device_info["id"],
            "type": device_info["type"],
            "traits": device_info["traits"],
            "name": device_info["name"],
            "willReportState": device_info["willReportState"]
        })

    return jsonify({
        "requestId": request_id,
        "payload": {
            "agentUserId": agent_user_id,
            "devices": devices
        }
    })


def handle_query(request_id, payload):
    device_states = {}
    for device in payload.get('devices', []):
        device_id = device['id']
        if device_id in MOCK_DEVICES:
            device_states[device_id] = MOCK_DEVICES[device_id]['state']

    return jsonify({
        "requestId": request_id,
        "payload": {
            "devices": device_states
        }
    })

def handle_execute(request_id, payload):
    commands = []

    for command in payload.get('commands', []):
        for device in command.get('devices', []):
            device_id = device['id']
            if device_id in MOCK_DEVICES:
                for execution in command.get('execution', []):
                    if execution['command'] == 'action.devices.commands.OnOff':
                        new_state = execution['params']['on']
                        MOCK_DEVICES[device_id]['state']['on'] = new_state
                        print(f"Device {device_id} turned {'On' if new_state else 'Off'}")

                commands.append({
                    "ids": [device_id],
                    "status": "SUCCESS",
                    "states": MOCK_DEVICES[device_id]['state']
                })
            else:
                commands.append({
                    "ids": [device_id],
                    "status": "ERROR",
                    "errorCode": "deviceNotFound"
                })

    return jsonify({
        "requestId": request_id,
        "payload": {
            "commands": commands
        }
    })
