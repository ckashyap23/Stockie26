import csv
import json

from app.backfill import labels, run

FEATURES = (
    "signal_date,next_trade_date,actual_trade_label,actual_trade_label_relaxed\n"
    "2026-10-01,2026-10-09,CALL,BOTH\n"   # data gap: the row after Oct 1 is Oct 9
    "2026-10-09,2026-10-12,PUT,PUT\n"
    "2026-10-12,2026-10-13,NO_POSITION,CALL\n"
    "2026-10-13,2026-10-14,,\n"            # last rows of a dataset have no label yet
    "2026-10-30,2026-11-02,CALL,CALL\n"
    "2026-11-02,2026-11-03,PUT,PUT\n"
)
# The prediction file has no row signalled on 2026-11-02, so its own next_trade_date for Oct 30 skips Monday Nov 2.
PREDICTIONS = (
    "signal_date,next_trade_date,final_prediction,effective_prediction\n"
    "2026-10-01,2026-10-09,CALL,CALL\n"
    "2026-10-09,2026-10-12,PUT,NO_POSITION\n"
    "2026-10-12,2026-10-13,NO_POSITION,PUT\n"
    "2026-10-30,2026-11-03,CALL,CALL\n"
)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _features(tmp_path):
    return _write(tmp_path, "features.csv", FEATURES)


def test_labels_are_keyed_by_next_trade_date_not_signal_date(tmp_path):
    found = labels.load_labels(_features(tmp_path))
    # the score for Oct 12 predicts Oct 12, so it takes the row that was SIGNALLED on Oct 9 (trade entered at Oct 12's open)
    assert found["2026-10-12"] == {"actual_trade_label": "PUT", "actual_trade_label_relaxed": "PUT", "label_from_signal_date": "2026-10-09"}
    assert found["2026-10-09"]["actual_trade_label"] == "CALL" and found["2026-10-09"]["label_from_signal_date"] == "2026-10-01"
    assert "2026-10-01" not in found  # a row signalled on Oct 1 never labels Oct 1


def test_dates_without_a_row_or_without_a_label_stay_blank(tmp_path):
    rows = {"2026-10-06": {"date": "2026-10-06"}, "2026-10-09": {"date": "2026-10-09"}, "2026-10-14": {"date": "2026-10-14"}}
    matched, missing = labels.apply_labels(rows, labels.load_labels(_features(tmp_path)))
    assert matched == 1 and missing == ["2026-10-06", "2026-10-14"]  # gap day, and a row whose label is empty
    assert rows["2026-10-09"]["actual_trade_label"] == "CALL"
    assert rows["2026-10-06"]["actual_trade_label"] == "" and rows["2026-10-06"]["label_from_signal_date"] == ""


def test_prediction_trade_date_comes_from_the_features_calendar(tmp_path):
    preds = labels.load_predictions(_write(tmp_path, "pred.csv", PREDICTIONS), _features(tmp_path))
    assert preds["2026-10-12"] == {"effective_prediction": "NO_POSITION", "prediction_from_signal_date": "2026-10-09"}
    # Friday Oct 30's prediction belongs to Monday Nov 2 even though the prediction file says Nov 3
    assert preds["2026-11-02"] == {"effective_prediction": "CALL", "prediction_from_signal_date": "2026-10-30"}
    assert "2026-11-03" not in preds  # no prediction was made on Nov 2, so Nov 3 stays blank rather than borrowing Friday's


def test_prediction_for_a_signal_date_missing_from_the_features_calendar_falls_back_to_the_files_own_date(tmp_path):
    extra = PREDICTIONS + "2026-12-01,2026-12-02,CALL,PUT\n"
    preds = labels.load_predictions(_write(tmp_path, "pred.csv", extra), _features(tmp_path))
    assert preds["2026-12-02"]["effective_prediction"] == "PUT"


def test_cli_updates_csv_and_state_and_survives_a_rewrite(tmp_path):
    data_dir = tmp_path / "bf"
    data_dir.mkdir()
    dates = ("2026-10-09", "2026-10-12", "2026-10-07", "2026-11-02", "2026-11-03")
    run._save_state(data_dir / "backfill_state.json", {"fetched": {}, "rows": {d: {"date": d, "score": 0.1} for d in dates}})
    args = ["--features", str(_features(tmp_path)), "--predictions", str(_write(tmp_path, "pred.csv", PREDICTIONS)),
            "--data-dir", str(data_dir)]
    assert labels.main(args) == 0
    rows = {r["date"]: r for r in csv.DictReader((data_dir / "backfill_scores.csv").open(encoding="utf-8-sig"))}
    assert rows["2026-10-12"]["actual_trade_label"] == "PUT" and rows["2026-10-12"]["effective_prediction"] == "NO_POSITION"
    assert rows["2026-10-07"]["actual_trade_label"] == "" and rows["2026-10-07"]["effective_prediction"] == ""
    assert rows["2026-10-09"]["label_from_signal_date"] == "2026-10-01"
    assert rows["2026-11-02"]["effective_prediction"] == "CALL" and rows["2026-11-03"]["effective_prediction"] == ""
    assert rows["2026-11-03"]["actual_trade_label"] == "PUT"  # the label still exists for Nov 3 (signalled Nov 2)
    # a later backfill run rewrites the CSV from the saved state: the extra columns must survive that
    run._write_csv(data_dir / "backfill_scores.csv", json.loads((data_dir / "backfill_state.json").read_text()))
    again = {r["date"]: r for r in csv.DictReader((data_dir / "backfill_scores.csv").open(encoding="utf-8-sig"))}
    assert again["2026-10-12"]["actual_trade_label"] == "PUT" and again["2026-10-12"]["effective_prediction"] == "NO_POSITION"
    assert list(again["2026-10-12"].keys())[-5:] == labels.LABEL_COLUMNS + labels.PREDICTION_COLUMNS


def test_cli_reports_a_missing_features_file_and_skips_a_missing_predictions_file(tmp_path):
    assert labels.main(["--features", str(tmp_path / "nope.csv"), "--data-dir", str(tmp_path)]) == 2
    data_dir = tmp_path / "bf"
    data_dir.mkdir()
    run._save_state(data_dir / "backfill_state.json", {"fetched": {}, "rows": {"2026-10-12": {"date": "2026-10-12"}}})
    assert labels.main(["--features", str(_features(tmp_path)), "--predictions", str(tmp_path / "none.csv"), "--data-dir", str(data_dir)]) == 0
