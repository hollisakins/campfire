"""campfire.transient: which errors are retried and how (issue #666)."""
import httpx
import pytest
import requests
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from campfire import transient as T


@pytest.mark.parametrize('exc', [
    httpx.RemoteProtocolError('Server disconnected'),
    httpx.ReadTimeout('timed out'),
    httpx.ConnectError('refused'),
    requests.ConnectionError('reset'),
    requests.Timeout('slow'),
    EndpointConnectionError(endpoint_url='https://osn'),
    ReadTimeoutError(endpoint_url='https://osn'),
    ConnectionResetError(54, 'Connection reset by peer'),
    BrokenPipeError(),
    TimeoutError(),
    ClientError({'Error': {'Code': 'SlowDown', 'Message': 'x'}, 'ResponseMetadata': {'HTTPStatusCode': 503}}, 'PutObject'),
    ClientError({'Error': {'Code': 'InternalError', 'Message': 'x'}, 'ResponseMetadata': {'HTTPStatusCode': 500}}, 'CopyObject'),
])
def test_transport_errors_are_transient(exc):
    assert T.is_transient(exc)


@pytest.mark.parametrize('exc', [
    RuntimeError('dr_archive_collect_begin: k is still referenced'),
    ValueError('bad'),
    httpx.HTTPStatusError('403', request=httpx.Request('GET', 'https://x'), response=httpx.Response(403)),
    ClientError({'Error': {'Code': 'PreconditionFailed', 'Message': 'x'}, 'ResponseMetadata': {'HTTPStatusCode': 412}}, 'PutObject'),
    ClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'x'}, 'ResponseMetadata': {'HTTPStatusCode': 404}}, 'GetObject'),
    ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'x'}, 'ResponseMetadata': {'HTTPStatusCode': 403}}, 'DeleteObjects'),
])
def test_answers_are_not_transient(exc):
    assert not T.is_transient(exc)


def test_retry_succeeds_after_transient_errors_with_backoff():
    calls, naps, logs = [], [], []
    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.RemoteProtocolError('Server disconnected')
        return 'ok'
    assert T.retry_transient(fn, what='the page', log=logs.append, sleep=naps.append, backoff=0.5) == 'ok'
    assert len(calls) == 3 and naps == [0.5, 1.0]
    assert len(logs) == 2 and 'attempt 1 of 3' in logs[0] and 'the page' in logs[0]


def test_retry_gives_up_after_the_last_attempt():
    calls = []
    def fn():
        calls.append(1)
        raise ConnectionResetError(54, 'reset')
    with pytest.raises(ConnectionResetError):
        T.retry_transient(fn, attempts=4, sleep=lambda s: None)
    assert len(calls) == 4


def test_retry_raises_a_non_transient_error_at_once():
    calls = []
    def fn():
        calls.append(1)
        raise RuntimeError('refused')
    with pytest.raises(RuntimeError):
        T.retry_transient(fn, sleep=lambda s: None)
    assert len(calls) == 1
