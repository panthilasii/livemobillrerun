"""Robustness tests for the hook-status probe.

Covers the multi-pattern signature parser introduced in 1.7.5 to
fix the false-negative "ยังไม่ Patch" reports on Android 11+
devices (Redmi Note 12 / HyperOS / MIUI 14, etc.) where ``dumpsys
package`` no longer prints the legacy ``signatures:[hex]`` form.

Also covers:

* Per-device baseline matching (``expected_fingerprint``) — exact
  match is the only fully-reliable patched signal across OEM ROMs.
* Preferred package routing (``expected_package``) — phones with
  multiple TikTok variants installed must probe the customer's
  KNOWN variant, not the first one in the canonical priority list.
* className backup signal — kicks in only when signature
  extraction returns nothing (confirmed via call-count assertions
  to keep the per-tick cost flat for the common path).
* Multiple LSPatch keystore prefixes — both legacy NP Create and
  upstream LSPatch builds detect as patched.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from src import hook_status as hs


def _result(rc, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["adb"], returncode=rc, stdout=stdout, stderr=stderr,
    )


class _Sequence:
    """Replay-by-call mock for ``subprocess.run``."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if not self.queue:
            raise AssertionError(
                f"unexpected adb call: {args!r} (queue empty)"
            )
        nxt = self.queue.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


# ── _extract_fingerprint: parser unit tests ───────────────────────


class TestExtractFingerprint:
    def test_legacy_signatures_bracket_form(self):
        out = "    signatures:[e0b8d3e51234abcd]"
        assert hs._extract_fingerprint(out) == "e0b8d3e51234abcd"

    def test_android_9_10_packagesignatures(self):
        # Real Android 9-10 dumpsys output shape.
        out = (
            "    signatures=PackageSignatures{abcd1234 "
            "[e0b8d3e51234abcd5678]}"
        )
        assert hs._extract_fingerprint(out) == "e0b8d3e51234abcd5678"

    def test_android_11_signinginfo_block(self):
        # Android 11+ wraps signatures in signingInfo.
        out = (
            "    signingInfo:\n"
            "      PackageSignatures{abcd1234 "
            "[e0b8d3e51234abcd5678]}"
        )
        assert hs._extract_fingerprint(out) == "e0b8d3e51234abcd5678"

    def test_android_12_signers_array(self):
        # Android 12+ scheme-v3 multi-signer format.
        out = (
            "    signingInfo:\n"
            "      PackageSignatures{\n"
            "          schemeVersion: 3\n"
            "          signers: [e0b8d3e51234abcd5678, dead]\n"
            "      }"
        )
        assert hs._extract_fingerprint(out) == "e0b8d3e51234abcd5678"

    def test_samsung_cert_digests(self):
        # Some Samsung One UI ROMs print ``cert digests``.
        out = (
            "    cert digests:\n"
            "        [e0b8d3e51234abcd5678]"
        )
        assert hs._extract_fingerprint(out) == "e0b8d3e51234abcd5678"

    def test_packagesignature_singular(self):
        # Some MIUI variants drop the "s" and just emit one
        # ``PackageSignature{}`` block per signer.
        out = "    PackageSignature{e0b8d3e51234abcd5678 v3}"
        assert hs._extract_fingerprint(out) == "e0b8d3e51234abcd5678"

    def test_picks_longest_when_multiple_hex(self):
        # Mixed output where multiple patterns match — we want
        # the longest, since shorter hex tokens are usually
        # version codes / flags, not the actual sig fingerprint.
        out = (
            "abcd1234 "
            "PackageSignatures{abcd1234 [e0b8d3e51234abcd5678]}"
        )
        assert hs._extract_fingerprint(out) == "e0b8d3e51234abcd5678"

    def test_empty_input(self):
        assert hs._extract_fingerprint("") == ""

    def test_no_hex_strings(self):
        assert hs._extract_fingerprint("not a signature line") == ""

    def test_lowercases_output(self):
        # User's actual report case: dumpsys printed uppercase hex.
        out = "    signatures:[E0B8D3E51234ABCD]"
        assert hs._extract_fingerprint(out) == "e0b8d3e51234abcd"


# ── probe end-to-end with new dumpsys formats ──────────────────────


class TestProbeAndroid11PlusFormats:
    """The bug we shipped 1.7.5 to fix: probes that returned
    "unpatched" on Redmi Note 12 / Android 13 because the regex
    didn't match the new ``signers: [...]`` form."""

    def test_signers_array_detects_patched(self):
        sig_block = (
            "    signingInfo:\n"
            "      PackageSignatures{\n"
            "          schemeVersion: 3\n"
            "          signers: [e0b8d3e51234abcd5678]\n"
            "      }"
        )
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, sig_block),
            _result(0, "    versionName=39.5.4"),
            _result(0, "98765\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.installed
        assert r.patched is True, (
            "Android 12+ signers: [...] format must detect as patched"
        )
        assert r.running is True
        assert r.version_name == "39.5.4"

    def test_packagesignatures_block_detects_patched(self):
        sig_block = (
            "    signingInfo:\n"
            "      PackageSignatures{abcd1234 [e0b8d3e51234abcd5678]}"
        )
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, sig_block),
            _result(0, "    versionName=39.5.4"),
            _result(0, "98765\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.patched is True


class TestProbeExactFingerprintMatch:
    """``expected_fingerprint`` is the per-device baseline. Exact
    match should win over prefix matching (more reliable when
    LSPatch keystores rotate between releases)."""

    def test_exact_match_overrides_prefix(self):
        # Custom keystore with no known prefix — should still
        # detect as patched because we passed the exact baseline.
        custom_fp = "ff00ff00deadbeef9999"
        sig_block = f"    signatures:[{custom_fp}]"
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, sig_block),
            _result(0, "    versionName=39.5.4"),
            _result(0, "12345\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe(
                "adb",
                expected_fingerprint=custom_fp,
            )
        assert r.patched is True, (
            "exact-match against expected_fingerprint must win"
        )

    def test_no_baseline_falls_back_to_prefix_list(self):
        # No baseline given — prefix matching against the known
        # NP-Create LSPatch keystore prefix kicks in.
        prefix = hs._KNOWN_LSPATCH_FINGERPRINT_PREFIXES[0]
        sig_block = f"    signatures:[{prefix}abcdef0123]"
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, sig_block),
            _result(0, "    versionName=39.5.4"),
            _result(0, "12345\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.patched is True, (
            "known LSPatch keystore prefix should still detect"
        )

    def test_baseline_mismatch_with_known_prefix_still_patches(self):
        # The on-device fingerprint matches a known LSPatch
        # prefix but NOT the per-device baseline (e.g. customer
        # re-patched from another machine after we recorded
        # baseline). We should still report patched=True — the
        # APK clearly came from LSPatch.
        sig_block = "    signatures:[e0b8d3e5fffffffe]"
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, sig_block),
            _result(0, "    versionName=39.5.4"),
            _result(0, "12345\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe(
                "adb",
                expected_fingerprint="e0b8d3e5aaaaaaaa",
            )
        assert r.patched is True


class TestProbePreferredPackage:
    """Customer with both regular TikTok and TikTok Lite installed.
    Without per-device package preference we'd probe the wrong
    variant and report it as unpatched."""

    def test_routes_to_recorded_variant(self):
        # Both installed; canonical order would pick "trill" but
        # the recorded variant is Lite.
        seq = _Sequence(
            _result(0,
                "package:com.ss.android.ugc.trill\n"
                "package:com.zhiliaoapp.musically.go\n",
            ),
            _result(0, "    signatures:[e0b8d3e5cafebabe]"),
            _result(0, "    versionName=39.0.0"),
            _result(0, "11111\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe(
                "adb",
                expected_package="com.zhiliaoapp.musically.go",
            )
        assert r.package == "com.zhiliaoapp.musically.go"

    def test_falls_back_to_canonical_order_when_recorded_uninstalled(self):
        # Recorded variant is no longer installed (customer
        # uninstalled it) — fall through to canonical order.
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, "    signatures:[e0b8d3e5cafebabe]"),
            _result(0, "    versionName=39.0.0"),
            _result(0, "11111\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe(
                "adb",
                expected_package="com.zhiliaoapp.musically.go",
            )
        assert r.package == "com.ss.android.ugc.trill"


class TestProbeClassNameBackup:
    """The className backup probe is the safety net for devices
    where signature extraction succeeds but yields a hex string
    that doesn't match a known LSPatch keystore prefix (HyperOS,
    OneUI 6, OEM keystore-rotation forks, etc.). Before 1.7.8 we
    only ran this probe when the sig parse returned nothing — but
    that path missed the most common failure mode in the field
    (sig parse returns a *non-LSPatch* hex), so 1.7.8 widened the
    rule to "always run when patched=False". These tests pin the
    new behaviour."""

    def test_className_backup_kicks_in_when_sig_empty(self):
        # Empty sig dump — backup className probe sees the loader.
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, ""),  # sig: empty
            _result(0, "    versionName=39.5.4"),
            _result(1, ""),  # not running
            _result(0,
                "    className=org.lsposed.lspatch.loader.LSPLoader\n"
            ),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.patched is True, (
            "className backup should detect LSPatch loader"
        )

    def test_className_backup_runs_even_with_unmatched_fingerprint(self):
        """1.7.8 regression test: HyperOS / OneUI dumpsys output
        sometimes makes ``_extract_fingerprint`` return a non-LSPatch
        hex string (e.g. it caught an APK ID instead of the cert
        digest). The previous "skip className when fingerprint
        non-empty" optimisation reported these patched phones as
        unpatched. The probe must now ALWAYS consult the className
        when the fingerprint check came up negative."""
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, "    signatures:[deadbeef12345678]"),  # parses, no LSPatch
            _result(0, "    versionName=39.5.4"),
            _result(1, ""),
            _result(0,
                "    appComponentFactory="
                "org.lsposed.lspatch.metaloader.LSPAppComponentFactory\n"
            ),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.patched is True, (
            "className backup must catch LSPatch even when sig parse "
            "extracted an unrelated hex string"
        )
        # The full call sequence must include the 5th (className) probe.
        assert len(seq.calls) == 5, (
            f"expected 5 adb calls (incl. className probe), saw {len(seq.calls)}"
        )

    def test_className_backup_no_loader_means_unpatched(self):
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, ""),
            _result(0, "    versionName=39.5.4"),
            _result(1, ""),
            _result(0, "    className=com.ss.android.ugc.aweme.MainApp\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.patched is False

    def test_className_backup_skipped_when_already_patched(self):
        """When the LSPatch prefix matches we've already proven the
        APK is patched — the extra className adb call would be wasted
        work. Locks in the short-circuit so a future refactor doesn't
        accidentally double the per-probe latency."""
        prefix = hs._KNOWN_LSPATCH_FINGERPRINT_PREFIXES[0]
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, f"    signatures:[{prefix}cafebabe]"),
            _result(0, "    versionName=39.5.4"),
            _result(0, "98765\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.patched is True
        assert len(seq.calls) == 4, (
            "className probe must NOT run when LSPatch prefix already matched"
        )


class TestProbeKnownLSPatchPrefixes:
    """Each prefix in the known list must detect as patched."""

    @pytest.mark.parametrize("prefix", hs._KNOWN_LSPATCH_FINGERPRINT_PREFIXES)
    def test_prefix_detects_patched(self, prefix):
        sig_block = f"    signatures:[{prefix}deadbeef]"
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, sig_block),
            _result(0, "    versionName=39.5.4"),
            _result(0, "12345\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.patched is True, (
            f"prefix {prefix!r} should be detected as LSPatch"
        )
