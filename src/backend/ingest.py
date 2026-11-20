import json
import os
from datetime import datetime, timezone
from decimal import Decimal
import boto3

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])

def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    if method == "POST":
        try:
            body = json.loads(event.get("body") or "{}", parse_float=Decimal)
            device_id = body.get("device_id") or body.get("deviceId")
            if not device_id:
                return _response(400, {"error": "Missing device_id"})

            item = {
                "device_id": str(device_id),
                "timestamp": body.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                "event_type": body.get("event_type", "sensor_batch"),
                "value": body.get("value", Decimal("0")),
                "unit": body.get("unit", "m/s^2"),
                "metadata": body.get("metadata", {})
            }
            table.put_item(Item=item)
            return _response(200, {"status": "stored", "device_id": item["device_id"]})
        except Exception as e:
            return _response(500, {"error": str(e)})

    if method == "GET":
        result = table.scan(Limit=50)
        return _response(200, {"events": result.get("Items", [])})

    return _response(405, {"error": "Method not allowed"})

def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str)
    }
