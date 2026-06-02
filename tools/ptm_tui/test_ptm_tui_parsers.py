import sys
import tempfile
import types
import unittest
from collections import deque
from pathlib import Path


class _Dummy:
    Pressed = type("Pressed", (), {})

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @classmethod
    def __class_getitem__(cls, item):
        return cls


def _install_ui_stubs() -> None:
    rich_module = types.ModuleType("rich")
    rich_text_module = types.ModuleType("rich.text")
    rich_text_module.Text = _Dummy
    sys.modules.setdefault("rich", rich_module)
    sys.modules.setdefault("rich.text", rich_text_module)

    textual_module = types.ModuleType("textual")
    textual_app_module = types.ModuleType("textual.app")
    textual_app_module.App = _Dummy
    textual_app_module.ComposeResult = object

    textual_containers_module = types.ModuleType("textual.containers")
    textual_containers_module.Horizontal = _Dummy
    textual_containers_module.Vertical = _Dummy
    textual_containers_module.VerticalScroll = _Dummy

    textual_reactive_module = types.ModuleType("textual.reactive")
    textual_reactive_module.reactive = lambda value: value

    textual_widgets_module = types.ModuleType("textual.widgets")
    for name in ["Button", "Checkbox", "DataTable", "Footer", "Header", "RichLog", "Select", "Static"]:
        setattr(textual_widgets_module, name, _Dummy)

    sys.modules.setdefault("textual", textual_module)
    sys.modules.setdefault("textual.app", textual_app_module)
    sys.modules.setdefault("textual.containers", textual_containers_module)
    sys.modules.setdefault("textual.reactive", textual_reactive_module)
    sys.modules.setdefault("textual.widgets", textual_widgets_module)

try:
    from ptm_tui import (
        HISTORY_LIMIT,
        append_history,
        append_telemetry_histories,
        apply_bot_entry_to_args,
        can_switch_bot,
        load_bot_entries,
        parse_labeled_tuple_section,
        parse_pmsptm_line,
        parse_ptm_section,
        parse_ptmcur_line,
        parse_ptmpos_line,
        parse_real_telemetry_line,
    )
except ModuleNotFoundError as exc:
    if exc.name in {"textual", "rich"}:
        _install_ui_stubs()
        from ptm_tui import (
            HISTORY_LIMIT,
            append_history,
            append_telemetry_histories,
            apply_bot_entry_to_args,
            can_switch_bot,
            load_bot_entries,
            parse_labeled_tuple_section,
            parse_pmsptm_line,
            parse_ptm_section,
            parse_ptmcur_line,
            parse_ptmpos_line,
            parse_real_telemetry_line,
        )
    else:
        raise


class TelemetryParserTests(unittest.TestCase):
    def test_ptmcur_formats(self) -> None:
        examples = [
            "PTMcur: {I1:2.41, I2:0.12, I3:7.8, I4:2.2}",
            "PTMcur: {'I1': 2.41, 'I2': 0.12, 'I3': 7.8, 'I4': 2.2}",
            'PTMcur: {"I1":2.41,"I2":0.12,"I3":7.8,"I4":2.2}',
            "noise PTMcur: {I1: 2410, I2: 120, I3: 7800, I4: 2200} trailing",
        ]
        for example in examples:
            parsed = parse_ptmcur_line(example)
            self.assertIsNotNone(parsed, example)
            self.assertEqual(set(parsed or {}), {"I1", "I2", "I3", "I4"})

    def test_ptmpos_formats(self) -> None:
        examples = [
            "PTMpos: {P1:42, P2:38, P3:40, P4:41}",
            "PTMpos: {'P1': 42, 'P2': 38, 'P3': 40, 'P4': 41}",
            'PTMpos: {"P1":42,"P2":38,"P3":40,"P4":41}',
        ]
        for example in examples:
            parsed = parse_ptmpos_line(example)
            self.assertIsNotNone(parsed, example)
            self.assertEqual(set(parsed or {}), {"P1", "P2", "P3", "P4"})

    def test_pmsptm_formats(self) -> None:
        examples = [
            'PMSPTM: {"I":2.1,"V":23.8,"battery":87,"relayPTM":true}',
            "PMSPTM: {'I': 2.1, 'V': 23.8, 'battery': 87, 'relayPTM': True}",
            "PMSPTM: {I:2.1, V:23.8, battery:87, relayPTM:true}",
        ]
        for example in examples:
            parsed = parse_pmsptm_line(example)
            self.assertIsNotNone(parsed, example)
            self.assertEqual(parsed.get("relayPTM"), True)

    def test_real_pmsptm_labeled_tuple(self) -> None:
        parsed = parse_pmsptm_line("PMSPTM:  (0)time (0)V (0)I (0)battery (0)relayPTM")
        self.assertEqual(parsed["time"], 0)
        self.assertEqual(parsed["V"], 0)
        self.assertEqual(parsed["I"], 0)
        self.assertEqual(parsed["battery"], 0)
        self.assertEqual(parsed["relayPTM"], False)

    def test_real_pms_labeled_tuple(self) -> None:
        parsed = parse_labeled_tuple_section(
            " (1.7800655e+09)time (23.176)V (0.84200007)I (51.37778)battery "
            "(34.27)temp (27)RH (1)relay1 (1)relay2"
        )
        self.assertEqual(parsed["V"], 23.176)
        self.assertEqual(parsed["I"], 0.84200007)
        self.assertEqual(parsed["battery"], 51.37778)
        self.assertEqual(parsed["relay1"], True)
        self.assertEqual(parsed["relay2"], True)

    def test_real_ptm_tuple(self) -> None:
        currents, positions = parse_ptm_section("PTM:  (0,0,0.001,0)I (56,60,82,61)P")
        self.assertEqual(currents, {"I1": 0, "I2": 0, "I3": 0.001, "I4": 0})
        self.assertEqual(positions, {"P1": 56, "P2": 60, "P3": 82, "P4": 61})

    def test_real_full_line(self) -> None:
        line = (
            "[2026-05-29 15:39:31:904] nx9; PMSPTM:  (0)time (0)V (0)I (0)battery (0)relayPTM; "
            "PMS:  (1.7800655e+09)time (23.176)V (0.84200007)I (51.37778)battery "
            "(34.27)temp (27)RH (1)relay1 (1)relay2; "
            "Position:  (1.7800655e+09)time (0)speed (0)dist (0)dir; "
            "PTM:  (0,0,0.001,0)I (56,60,82,61)P; "
            "Pump:  (0,0)rpm; Drive:  (0,0)rpm (38.25,36.93)temp"
        )
        parsed = parse_real_telemetry_line(line)
        self.assertEqual(parsed.currents["I3"], 0.001)
        self.assertEqual(parsed.positions, {"P1": 56, "P2": 60, "P3": 82, "P4": 61})
        self.assertEqual(parsed.pms_fallback["V"], 23.176)
        self.assertEqual(parsed.pms["relayPTM"], False)

    def test_load_bot_config_options(self) -> None:
        entries = load_bot_entries_from_text(
            '[{"id":"BRP08","host":"192.168.2.48","user":"aceng","ssh_port":2222,'
            '"password":"pw","accept_new_host_key":true}]'
        )
        self.assertEqual(entries[0].id, "BRP08")
        self.assertEqual(entries[0].host, "192.168.2.48")
        self.assertEqual(entries[0].ssh_port, 2222)
        self.assertEqual(entries[0].password, "pw")
        self.assertEqual(entries[0].accept_new_host_key, True)

    def test_apply_bot_entry_updates_active_connection(self) -> None:
        entries = load_bot_entries_from_text('[{"id":"BRP09","host":"192.168.2.49","user":"aceng"}]')
        args = types.SimpleNamespace()
        apply_bot_entry_to_args(args, entries[0])
        self.assertEqual(args.host, "192.168.2.49")
        self.assertEqual(args.selected_bot_id, "BRP09")

    def test_history_buffer_caps_at_limit(self) -> None:
        history = deque(maxlen=HISTORY_LIMIT)
        for value in range(HISTORY_LIMIT + 10):
            append_history(history, value, HISTORY_LIMIT)
        self.assertEqual(len(history), HISTORY_LIMIT)
        self.assertEqual(history[0], 10)

    def test_ptm_current_updates_histories(self) -> None:
        parsed = parse_real_telemetry_line("PTM:  (0,0,0.001,0)I (56,60,82,61)P")
        histories = {key: deque(maxlen=HISTORY_LIMIT) for key in ["I1", "I2", "I3", "I4", "supply_v", "supply_i"]}
        append_telemetry_histories(parsed, histories, HISTORY_LIMIT)
        self.assertEqual(list(histories["I3"]), [0.001])

    def test_switch_blocked_when_ptm_started(self) -> None:
        self.assertFalse(can_switch_bot(True))
        self.assertTrue(can_switch_bot(False))


def load_bot_entries_from_text(text: str):
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "bots.json"
        path.write_text(text, encoding="utf-8")
        return load_bot_entries(path)


if __name__ == "__main__":
    unittest.main()
