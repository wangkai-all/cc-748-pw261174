#!/usr/bin/env python3
"""Entry point: python run.py [--host 127.0.0.1] [--port 8000]"""
from __future__ import annotations

import argparse

from app.server import make_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Construction schedule risk analysis server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"工期风险分析台运行中: http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.shutdown()


if __name__ == "__main__":
    main()
