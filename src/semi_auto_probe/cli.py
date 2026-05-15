from __future__ import annotations

import argparse

from .serial_client import ControllerSerialClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Semi Auto Probe controller utilities.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    test_parser = subparsers.add_parser("test", help="Run the controller communication feedback test.")
    test_parser.add_argument("--port", required=True, help="Serial port, for example COM3.")
    test_parser.add_argument("--timeout", type=float, default=1.0, help="Serial timeout in seconds.")

    args = parser.parse_args()

    if args.command == "test":
        client = ControllerSerialClient(port=args.port, timeout=args.timeout)
        try:
            result = client.communication_test()
        finally:
            client.close()

        print(f"TX: {result.request_hex}")
        print(f"RX: {result.response_hex or '-'}")
        print(f"OK: {result.ok}")
        print(result.message)
