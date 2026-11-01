# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Exact-input private native adapter; generated game code stays local.

Call only for a future stopped port, after SPU generation and before CMake.
None means unmatched input, with no filesystem changes. A matching input
fails before mutation on package/runtime/destination/CMake contract mismatch.
The returned source proof is not evidence of a compiled or playable game.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "verification/motorstorm-repair/private-adapter-package"
MANIFEST_SHA256 = "355f54ef507a3df09941091c5f1c3c1a8ba86dc7d71d4ac0b1158ef7af947429"
INPUT_SHA256 = "bbb7fe2545e54847483439f59cda54e907db23a111d7afe9a1b655624886b287"
RAW_OFFSET, RAW_SIZE = 0xFECC80, 11520
RAW_SHA256 = "145395f1434fbdbb7a9596795bf0cc439b691e25d624cee9d87c1de6aeddfd0d"
RAW_FNV = "093FC7EE6C9F7E84"
ADAPTER_ID = "motorstorm-pacific-rift-private-wws-v1"
CMAKE_LINE = 'target_sources(${PROJECT_NAME} PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/spu/motorstorm_policy_register.c")'

class PrivateNativeAdapterError(RuntimeError):
    pass

def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _verified(path, expected, label):
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PrivateNativeAdapterError(f"Private MotorStorm adapter: missing {label}: {path}") from exc
    if hashlib.sha256(data).hexdigest() != expected:
        raise PrivateNativeAdapterError(f"Private MotorStorm adapter: {label} hash mismatch: {path}")
    return data

def _under(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or ":" in relative or "\\" in relative:
        raise PrivateNativeAdapterError("Private MotorStorm adapter: unsafe package path")
    path = root / rel
    if not path.resolve().is_relative_to(root.resolve()):
        raise PrivateNativeAdapterError("Private MotorStorm adapter: path escapes its root")
    return path

def _fnv(data):
    # Match spu_workload_fingerprint exactly, including its nonstandard basis.
    value = 1469598103934665603
    for byte in data:
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016X}"

def apply(stage, elf, backend, project):
    """Apply to generated port directory; return a serializable proof or None.

    A caller may bind diagnostics to an executable only after successful build.
    All original-game-derived files remain in the private future attempt.
    """
    elf = Path(elf)
    try:
        input_hash = _sha(elf)
    except OSError as exc:
        raise PrivateNativeAdapterError(f"Cannot inspect native-build ELF: {elf}") from exc
    if input_hash != INPUT_SHA256:
        return None
    stage, backend, package = Path(stage), Path(backend), Path(PACKAGE_ROOT)
    try:
        with elf.open("rb") as stream:
            stream.seek(RAW_OFFSET)
            raw = stream.read(RAW_SIZE)
    except OSError as exc:
        raise PrivateNativeAdapterError("Private MotorStorm adapter: cannot read original policy") from exc
    if len(raw) != RAW_SIZE or hashlib.sha256(raw).hexdigest() != RAW_SHA256 or _fnv(raw) != RAW_FNV:
        raise PrivateNativeAdapterError("Private MotorStorm adapter: raw policy hash/fingerprint mismatch")
    manifest = json.loads(_verified(package / "manifest.json", MANIFEST_SHA256, "manifest"))
    if (manifest.get("schema") != 1 or manifest.get("adapter_id") != ADAPTER_ID or
            manifest.get("input_sha256") != input_hash or not manifest.get("private")):
        raise PrivateNativeAdapterError("Private MotorStorm adapter: manifest identity mismatch")
    # Complete all validation/conflict checks before creating anything.
    for relative, expected in manifest["backend_contract"].items():
        _verified(_under(backend, relative), expected, "supporting runtime contract")
    sources, destinations = {}, {}
    installed = dict(manifest["files"])
    for relative, expected in manifest["notices"].items():
        dest_relative = "private-native-adapter/" + relative
        sources[dest_relative] = _verified(_under(package, relative), expected, "private/license notice")
        installed[dest_relative] = expected
    for relative, expected in manifest["files"].items():
        sources[relative] = _verified(_under(package / "payload", relative), expected, "private source")
    for relative, expected in installed.items():
        dest = _under(stage, relative)
        if dest.exists() and (not dest.is_file() or _sha(dest) != expected):
            raise PrivateNativeAdapterError(f"Private MotorStorm adapter: preserve changed destination: {dest}")
        parent = dest.parent
        while parent != stage:
            if parent.exists() and not parent.is_dir():
                raise PrivateNativeAdapterError(f"Private MotorStorm adapter: invalid destination directory: {parent}")
            parent = parent.parent
        destinations[relative] = dest
    if sources["spu/motorstorm_wws_policy/code.bin"] != raw:
        raise PrivateNativeAdapterError("Private MotorStorm adapter: package differs from original policy")
    lift = json.loads(sources["spu/motorstorm_wws_policy/lift.json"])
    if len(lift.get("functions", [])) != 188 or lift.get("unsupported") or not any(b[0] == 0xA2C for b in lift["functions"]):
        raise PrivateNativeAdapterError("Private MotorStorm adapter: lift coverage/interrupt-entry mismatch")
    cmake = _under(stage, "CMakeLists.txt")
    try:
        before = cmake.read_bytes()
        text = before.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise PrivateNativeAdapterError("Private MotorStorm adapter needs generated port CMakeLists.txt") from exc
    if (not re.search(r"project\s*\(", text, re.I) or '"${SPU_DIR}/*/spu_recomp.c"' not in text or
            not re.search(r"add_executable\s*\(\s*\$\{PROJECT_NAME\}", text, re.I)):
        raise PrivateNativeAdapterError("Private MotorStorm adapter: generated CMake contract mismatch")
    registrations = [s.strip() for s in text.splitlines() if "motorstorm_policy_register.c" in s and not s.lstrip().startswith("#")]
    if registrations and registrations != [CMAKE_LINE]:
        raise PrivateNativeAdapterError("Private MotorStorm adapter: conflicting CMake policy registration")
    if "motorstorm_multistream_register.c" in text:
        raise PrivateNativeAdapterError("Private MotorStorm adapter: stale fixed-address helper registration needs review")
    after = before if registrations else before + b"\n# Private exact-input WWS policy; keep owner-derived sources local.\n" + CMAKE_LINE.encode() + b"\n"
    if _sha(elf) != input_hash:
        raise PrivateNativeAdapterError("Private MotorStorm adapter: ELF changed during validation")
    for relative, dest in destinations.items():
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(sources[relative])
    if after != before:
        cmake.write_bytes(after)
    return {"schema": 1, "applied": True, "adapter_id": ADAPTER_ID, "version": 1,
            "project_id": project.get("id", "") if isinstance(project, dict) else str(project),
            "input_sha256": input_hash, "raw_sha256": RAW_SHA256, "raw_fingerprint": RAW_FNV,
            "manifest_sha256": MANIFEST_SHA256, "backend_contract": manifest["backend_contract"],
            "installed_sources": installed, "cmake_sha256": hashlib.sha256(after).hexdigest(),
            "stage": str(stage.resolve()), "private": True, "mode": "hybrid-SPU", "playability": "unverified",
            "diagnostic_env": dict(manifest["diagnostic_env"]), "provenance": manifest["provenance"]}
