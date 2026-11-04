"""Regenerate checked-in Python contracts with protoc (also runs during image build)."""

from pathlib import Path

import grpc_tools
from grpc_tools import protoc

root = Path(__file__).resolve().parents[1]
include = Path(grpc_tools.__file__).parent / "_proto"
result = protoc.main(
    [
        "protoc",
        f"-I{root / 'proto'}",
        f"-I{include}",
        f"--python_out={root / 'app/generated'}",
        f"--grpc_python_out={root / 'app/generated'}",
        str(root / "proto/flight.proto"),
    ]
)
if result:
    raise SystemExit(result)
path = root / "app/generated/flight_pb2_grpc.py"
path.write_text(
    path.read_text().replace(
        "import flight_pb2 as flight__pb2",
        "from . import flight_pb2 as flight__pb2",
    )
)
