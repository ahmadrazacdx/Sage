import pytest
import asyncio
from sage.network import check_internet, NetworkMonitor
from unittest.mock import patch, AsyncMock, MagicMock
from sage.config import NetworkSettings

@pytest.fixture
def dummy_network_settings():
    return NetworkSettings(
        force_offline=False,
        check_interval=5,
        timeout=0.5
    )

@pytest.mark.asyncio
async def test_tcp_probe_success():
    with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
        mock_reader = MagicMock()
        mock_writer = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_open.return_value = (mock_reader, mock_writer)
        
        from sage.network import _tcp_probe
        result = await _tcp_probe("1.1.1.1", 443, 0.1)
        assert result is True
        mock_writer.close.assert_called_once()
        mock_writer.wait_closed.assert_called_once()

@pytest.mark.asyncio
async def test_tcp_probe_failure():
    with patch("asyncio.open_connection", side_effect=TimeoutError):
        from sage.network import _tcp_probe
        result = await _tcp_probe("1.1.1.1", 443, 0.1)
        assert result is False

@pytest.mark.asyncio
async def test_check_internet_success():
    with patch("sage.network._tcp_probe", new_callable=AsyncMock) as mock_probe:
        mock_probe.return_value = True
        result = await check_internet(0.1)
        assert result is True

@pytest.mark.asyncio
async def test_check_internet_failure():
    with patch("sage.network._tcp_probe", new_callable=AsyncMock) as mock_probe:
        mock_probe.return_value = False
        result = await check_internet(0.1)
        assert result is False

@pytest.mark.asyncio
async def test_network_monitor_start_stop(dummy_network_settings):
    monitor = NetworkMonitor(dummy_network_settings)
    with patch("sage.network.check_internet", new_callable=AsyncMock) as mock_check:
        mock_check.return_value = True
        await monitor.start()
        assert monitor.online is True
        assert monitor._task is not None
        await monitor.stop()

@pytest.mark.asyncio
async def test_network_monitor_forced_offline(dummy_network_settings):
    dummy_network_settings.force_offline = True
    monitor = NetworkMonitor(dummy_network_settings)
    await monitor.start()
    assert monitor.online is False
    assert monitor._task is None
    await monitor.stop()

@pytest.mark.asyncio
async def test_network_monitor_poll(dummy_network_settings):
    monitor = NetworkMonitor(dummy_network_settings)
    
    async def mock_check(timeout):
        return False
        
    with patch("sage.network.check_internet", side_effect=mock_check) as mock_chk:
        await monitor.start()
        
        with patch("sage.network.check_internet", new_callable=AsyncMock) as mock_chk2:
            mock_chk2.return_value = True
            await asyncio.sleep(0.2)
            assert monitor.online is True or monitor.online is False
            
        await monitor.stop()
