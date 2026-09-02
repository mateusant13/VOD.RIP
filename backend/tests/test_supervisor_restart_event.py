"""Regression checks for supervisor restart versus final shutdown."""

from services import server_lifecycle


def test_restart_stop_does_not_stop_supervisor() -> None:
    server_lifecycle._shutdown_event.clear()
    server_lifecycle.stop_api_server(port=None, wait_for_port=False, restart=True)
    assert not server_lifecycle.should_stop_supervisor()

    server_lifecycle.stop_api_server(port=None, wait_for_port=False)
    assert server_lifecycle.should_stop_supervisor()
    server_lifecycle._shutdown_event.clear()
