#!/usr/bin/env python3
"""Minimal pure-Python Protobuf encoder for binary Perfetto .pftrace (TracePacket / TrackDescriptor / TrackEvent).
Produces authentic multi-process, multi-thread binary .pftrace files readable by both Perfetto UI and trace_processor_shell.
"""
from typing import Optional


def _varint(n: int) -> bytes:
    out = bytearray()
    n &= 0xFFFFFFFFFFFFFFFF
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _tag(field_no: int, wire_type: int) -> bytes:
    return _varint((field_no << 3) | wire_type)


def _f_varint(field_no: int, val: int) -> bytes:
    return _tag(field_no, 0) + _varint(val)


def _f_bytes(field_no: int, data: bytes) -> bytes:
    return _tag(field_no, 2) + _varint(len(data)) + data


def _f_str(field_no: int, s: str) -> bytes:
    return _f_bytes(field_no, s.encode("utf-8"))


class PftraceWriter:
    """Encodes Perfetto TrackEvent trace packets into binary .pftrace."""

    def __init__(self, seq_id: int = 1001):
        self.seq_id = seq_id
        self.buf = bytearray()
        self._first = True

    def _emit_packet(self, payload: bytes) -> None:
        self.buf.extend(_f_bytes(1, payload))

    def add_process_track(self, uuid: int, pid: int, process_name: str) -> None:
        proc_desc = _f_varint(1, pid) + _f_str(6, process_name)
        td = _f_varint(1, uuid) + _f_bytes(3, proc_desc)
        pkt = _f_varint(10, self.seq_id) + _f_bytes(60, td)
        if self._first:
            pkt += _f_varint(13, 1)  # SEQ_INCREMENTAL_STATE_CLEARED
            self._first = False
        self._emit_packet(pkt)

    def add_thread_track(self, uuid: int, parent_uuid: int, pid: int, tid: int, thread_name: str) -> None:
        th_desc = _f_varint(1, pid) + _f_varint(2, tid) + _f_str(5, thread_name)
        td = _f_varint(1, uuid) + _f_varint(5, parent_uuid) + _f_bytes(4, th_desc)
        pkt = _f_varint(10, self.seq_id) + _f_bytes(60, td)
        self._emit_packet(pkt)

    def _debug_ann(self, name: str, value) -> bytes:
        ann = _f_str(10, name)
        if isinstance(value, bool):
            ann += _f_varint(2, 1 if value else 0)
        elif isinstance(value, int):
            ann += _f_varint(4, value)
        else:
            ann += _f_str(6, str(value))
        return ann

    def add_slice(
        self,
        track_uuid: int,
        ts_ns: int,
        dur_ns: int,
        name: str,
        category: str = "app",
        args: Optional[dict] = None,
    ) -> None:
        # TYPE_SLICE_BEGIN = 1
        te_begin = bytearray()
        te_begin.extend(_f_str(22, category))
        te_begin.extend(_f_str(23, name))
        te_begin.extend(_f_varint(9, 1))
        te_begin.extend(_f_varint(11, track_uuid))
        if args:
            for k, v in args.items():
                te_begin.extend(_f_bytes(4, self._debug_ann(k, v)))
        pkt_b = _f_varint(8, ts_ns) + _f_varint(10, self.seq_id) + _f_bytes(11, bytes(te_begin))
        self._emit_packet(pkt_b)

        # TYPE_SLICE_END = 2
        te_end = _f_varint(9, 2) + _f_varint(11, track_uuid)
        pkt_e = _f_varint(8, ts_ns + dur_ns) + _f_varint(10, self.seq_id) + _f_bytes(11, te_end)
        self._emit_packet(pkt_e)

    def to_bytes(self) -> bytes:
        return bytes(self.buf)
