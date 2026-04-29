import argparse

import pytest

from zelda.cli import (
    _max_results_type,
    _positive_int,
    build_parser,
)


# ── parser shape ────────────────────────────────────────────────────────


def test_parser_requires_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_rejects_unknown_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["bogus", "--city", "Ludhiana"])


# ── discover subcommand ────────────────────────────────────────────────


def test_discover_basic_args():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana"])

    assert args.command == "discover"
    assert args.city == "Ludhiana"
    assert args.max_results == 1  # default
    assert args.max_pages == 1     # default


def test_discover_requires_city():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover"])


def test_discover_max_results_int():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana", "--max-results", "5"])
    assert args.max_results == 5


def test_discover_max_results_all_means_unlimited():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana", "--max-results", "all"])
    assert args.max_results is None


def test_discover_max_results_zero_for_dry_run():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana", "--max-results", "0"])
    assert args.max_results == 0


def test_discover_max_results_rejects_negative():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover", "--city", "Ludhiana", "--max-results", "-1"])


def test_discover_max_results_rejects_garbage():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover", "--city", "Ludhiana", "--max-results", "lots"])


def test_discover_max_pages_default_one():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana"])
    assert args.max_pages == 1


def test_discover_max_pages_int():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana", "--max-pages", "3"])
    assert args.max_pages == 3


def test_discover_max_pages_rejects_zero():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover", "--city", "Ludhiana", "--max-pages", "0"])


# ── sync subcommand ───────────────────────────────────────────────────


def test_sync_basic_args():
    parser = build_parser()
    args = parser.parse_args(["sync", "--city", "Ludhiana"])
    assert args.command == "sync"
    assert args.city == "Ludhiana"


def test_sync_requires_city():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["sync"])


# ── argument-type helpers ─────────────────────────────────────────────


def test_max_results_type_accepts_all():
    assert _max_results_type("all") is None
    assert _max_results_type("ALL") is None
    assert _max_results_type("All") is None


def test_max_results_type_accepts_zero():
    assert _max_results_type("0") == 0


def test_max_results_type_accepts_positive_int():
    assert _max_results_type("42") == 42


def test_max_results_type_rejects_negative():
    with pytest.raises(argparse.ArgumentTypeError):
        _max_results_type("-1")


def test_max_results_type_rejects_float():
    with pytest.raises(argparse.ArgumentTypeError):
        _max_results_type("3.14")


def test_max_results_type_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        _max_results_type("lots")


def test_positive_int_accepts_one_or_more():
    assert _positive_int("1") == 1
    assert _positive_int("100") == 100


def test_positive_int_rejects_zero():
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("0")


def test_positive_int_rejects_negative():
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("-5")


def test_positive_int_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("xyz")
