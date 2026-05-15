from __future__ import annotations

import argparse

from .logging_utils import colorize_hex_frame, configure_logging, print_startup_banner
from .serial_client import ControllerSerialClient


def main() -> None:
    print_startup_banner()
    logger = configure_logging()

    parser = argparse.ArgumentParser(description="Semi Auto Probe controller utilities.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    test_parser = subparsers.add_parser("test", help="Run the controller communication feedback test.")
    test_parser.add_argument("--port", required=True, help="Serial port, for example COM3.")
    test_parser.add_argument("--timeout", type=float, default=1.0, help="Serial timeout in seconds.")

    args = parser.parse_args()

    if args.command == "test":
        client = ControllerSerialClient(port=args.port, timeout=args.timeout)
        logger.info("Running CLI communication test on %s.", args.port)
        try:
            result = client.communication_test()
        finally:
            client.close()

        print(colorize_hex_frame(result.request_hex, "TX"))
        print(colorize_hex_frame(result.response_hex or "-", "RX"))
        print(f"OK: {result.ok}")
        print(result.message)
        if result.ok:
            logger.info("CLI communication test passed.")
        else:
            logger.warning("CLI communication test failed: %s", result.message)
