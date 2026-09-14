from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_incident_tracker_persists_heartbeat_and_fatal_reason() -> None:
    source = (ROOT / "app/services/runtime_incident.py").read_text(encoding="utf-8")

    assert 'RUNTIME_STATE_KEY = "mimoru:runtime:state"' in source
    assert 'RUNTIME_FATAL_KEY = "mimoru:runtime:last_fatal"' in source
    assert "HEARTBEAT_INTERVAL_SECONDS = 10" in source
    assert '"processed_updates": self.processed_updates' in source
    assert 'reason = f"{type(exc).__name__}: {str(exc)[:1200]}"' in source
    assert '"clean_shutdown": clean_shutdown' in source


def test_runtime_incident_report_has_heading_quote_times_reason_and_counts() -> None:
    source = (ROOT / "app/services/runtime_incident.py").read_text(encoding="utf-8")

    assert '"🚨 Mimoru — аварийный перезапуск\\n\\n"' in source
    assert 'f"› Бот упал:' in source
    assert 'f"› Бот снова поднялся:' in source
    assert 'f"› Причина:' in source
    assert 'f"› Обработано до падения:' in source
    assert 'f"› Накопилось во время простоя:' in source
    assert 'f"› Восстановлено критических:' in source
    assert 'f"› Отброшено устаревших/обычных:' in source


def test_main_tracks_runtime_and_only_marks_clean_shutdown_for_normal_stop() -> None:
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert "RuntimeTracker(redis)" in source
    assert "RuntimeUpdateCounterMiddleware(runtime_tracker)" in source
    assert "dp.update.outer_middleware(runtime_counter_middleware)" in source
    assert "runtime_tracker.heartbeat_loop(stop_event)" in source
    assert "incident = await runtime_tracker.inspect_previous_run()" in source
    assert "await runtime_tracker.mark_started()" in source
    assert "notify_runtime_incident(" in source
    assert "settings.service_owner_ids" in source
    assert "incident" in source
    assert "backlog_stats" in source
    assert "await runtime_tracker.record_fatal(exc)" in source
    assert "await runtime_tracker.mark_clean_shutdown()" in source
    assert "except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):" in source
