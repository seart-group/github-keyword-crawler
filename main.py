#!/usr/bin/env python3

from argparse import ArgumentParser
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from http import HTTPStatus
from logging import getLogger as get_logger, Logger
from logging.config import fileConfig as logger_config_file
from os import environ as environment, makedirs
from os.path import join as path
from typing import Any, Callable, Final

from dateutil.parser import parse as parse_date
from flatdict import FlatDict
from github import Github, PaginatedList, UnknownObjectException
from interval import Interval
from pymongo import DESCENDING as DESC, MongoClient
from pymongo.results import InsertManyResult
from urllib3.response import BaseHTTPResponse
from urllib3.util.retry import Retry


def init_logger() -> Logger:
    tmpdir = environment.get('TMPDIR', '/tmp')
    logs_directory = path(tmpdir, 'gh-keyword-crawler')
    makedirs(logs_directory, exist_ok=True)
    logger_config_file('logger.ini')
    return get_logger(__name__)


logger: Final = init_logger()


def round_datetime(function: Callable[..., datetime]) -> Callable[..., datetime]:
    """
    Round the microsecond component of a datetime object
    returned by a decorated function to the nearest second.

    If the microsecond component is greater than or equal to 500000,
    the second component is rounded up by adding one second.

    :param function:
        A callable with any number of positional and
        keyword arguments that returns a datetime object.
    :type function: Callable[..., datetime]
    :returns:
        A new callable that wraps the input function
        and returns a rounded datetime object.
    :rtype: Callable[..., datetime]
    """
    @wraps(function)
    def _wrapper(*args, **kwargs):
        dt: datetime = function(*args, **kwargs)
        if dt.microsecond >= 500_000:
            dt += timedelta(seconds=1)
        return dt.replace(microsecond=0)
    return _wrapper


class TimeDifferenceTooSmallException(ValueError):
    """
    Exception raised when the timedelta between two datetime objects is less than a certain value.`
    """
    pass


class GitHubRetry(Retry):
    """
    Subclass of :py:class:`Retry` from the :py:mod:`urllib3` package,
    with additional functionality for handling rate limits and
    retrying requests made to the GitHub API.

    :ref:`Based on <https://github.com/PyGithub/PyGithub/issues/1989#issuecomment-1261656811>`
    """

    def __init__(self, *args, **kwargs):
        if len(args) < 2 and 'status_forcelist' not in kwargs:
            kwargs['status_forcelist'] = frozenset({
                HTTPStatus.FORBIDDEN.value,                 # 403
                HTTPStatus.TOO_MANY_REQUESTS.value,         # 429
                HTTPStatus.INTERNAL_SERVER_ERROR.value,     # 500
                HTTPStatus.NOT_IMPLEMENTED.value,           # 501
                HTTPStatus.BAD_GATEWAY.value,               # 502
                HTTPStatus.SERVICE_UNAVAILABLE.value,       # 503
                HTTPStatus.GATEWAY_TIMEOUT.value,           # 504
            })
        super(GitHubRetry, self).__init__(*args, **kwargs)

    def get_retry_after(self, response: BaseHTTPResponse):
        if response.status == HTTPStatus.FORBIDDEN.value:
            reset_header = response.headers['X-RateLimit-Reset']
            reset_time = datetime.fromtimestamp(int(reset_header))
            retry_after = max((reset_time - datetime.now()).total_seconds() + 1, 0)
            logger.info('Rate limit exceeded, sleeping for %s...', timedelta(seconds=retry_after))
            return retry_after
        else:
            logger.warning('Unexpected response status [%s], reverting to default retry behaviour...', response.status)
            super().get_retry_after(response)


class Miner:

    #: Maximum allowed page size offered by the GitHub API
    MAX_PAGE_SIZE: Final = 100

    #: Maximum number of results obtainable when performing API searches
    MAX_RESULT_COUNT: Final = MAX_PAGE_SIZE * 10

    #: The default string timestamp format
    TIMESTAMP_FORMAT: Final = '%Y-%m-%dT%H:%M:%SZ'

    def __init__(self, token: str, target: str, keyword: str):
        self._api = Github(
            login_or_token=token,
            retry=(GitHubRetry()),
            per_page=self.MAX_PAGE_SIZE,
        )
        self._client = MongoClient(
            appname=f'crawler-{target}-{keyword}',
            host=environment.get('DATABASE_HOST', 'localhost'),
            port=int(environment.get('DATABASE_PORT', '27017')),
        )
        self._database = self._client.get_database(keyword)
        self._collection = self._database[target]
        self._target = target
        self._keyword = keyword
        self._init_functions()
        self._init_queue()

    def _init_functions(self):
        setattr(self, '_search', self._init_search_function())
        setattr(self, '_store', self._init_store_function())

    def _init_search_function(self):
        match self._target:
            case 'commits':
                return lambda interval: self._api.search_commits(
                    query=f'{self._keyword} committer-date:{interval}',
                    sort='committer-date',
                    order='asc',
                )
            case 'issues' | 'pull-requests':
                return lambda interval: self._api.search_issues(
                    query=f'{self._keyword} created:{interval} is:{self._target[:-1]}',
                    sort='created',
                    order='asc',
                )
            case _:
                raise ValueError(f'Mining not implemented for \'{self._target}\'')

    def _init_store_function(self):
        return lambda results: self._collection.insert_many(results)

    def _convert(self, results: PaginatedList):
        converted = []
        for result in results:
            try:
                converted.append(result.raw_data)
            except UnknownObjectException as uoe:
                logger.warning('%s returned when requesting %s data: %s', uoe.status, self._target, uoe.data)
        return converted

    def _init_queue(self):
        self._queue = deque()
        interval = Interval.between(self._lower_date(), self._upper_date_default())
        self._queue.append(interval)

    @round_datetime
    def _lower_date(self) -> datetime:
        match self._target:
            case 'commits':
                path = 'commit.committer.date'
            case 'issues' | 'pull-requests':
                path = 'created_at'
            case _:
                raise ValueError(f'Mining not implemented for \'{self._target}\'')
        lower_search = self._collection.find(
            filter={},
            projection={'_id': 0, path: 1},
            sort=[(path, DESC)],
            limit=1,
        )
        lower_date_default_str = self._lower_date_default().strftime(Miner.TIMESTAMP_FORMAT)
        lower_date_default_doc = self._construct_dict(path, lower_date_default_str)
        lower_date_doc = next(lower_search, lower_date_default_doc)
        lower_date_str = self._destruct_dict(path, lower_date_doc)
        return parse_date(lower_date_str)

    @staticmethod
    def _construct_dict(path: str, value: Any) -> dict:
        fd = FlatDict({}, delimiter='.')
        fd[path] = value
        return fd.as_dict()

    @staticmethod
    def _destruct_dict(path: str, d: dict) -> Any:
        fd = FlatDict(d, delimiter='.')
        return fd[path]

    @staticmethod
    @round_datetime
    def _lower_date_default() -> datetime:
        return datetime(2022, 12, 1, tzinfo=timezone.utc)

    @staticmethod
    @round_datetime
    def _upper_date_default() -> datetime:
        return datetime.now(tz=timezone.utc)

    @staticmethod
    @round_datetime
    def _median_date(lower: datetime, upper: datetime) -> datetime:
        delta = upper - lower
        if delta.seconds <= 1:
            raise TimeDifferenceTooSmallException
        lower_ts = lower.timestamp()
        upper_ts = upper.timestamp()
        median_ts = (lower_ts + upper_ts) / 2
        return datetime.fromtimestamp(median_ts, tz=timezone.utc)

    def __call__(self, *args, **kwargs):
        logger.info('Mining %s containing keyword %s...', self._target, self._keyword)
        while len(self._queue):
            interval = self._queue.pop()
            lower = interval.lower_bound
            upper = interval.upper_bound
            lower_str = lower.strftime(Miner.TIMESTAMP_FORMAT)
            upper_str = upper.strftime(Miner.TIMESTAMP_FORMAT)
            interval_str = f'{lower_str}..{upper_str}'
            logger.info('Examining interval: %s', interval_str)
            results = self._search(interval_str)
            if results.totalCount == self.MAX_RESULT_COUNT:
                results[0]  # see: https://github.com/PyGithub/PyGithub/issues/1309
            total = results.totalCount
            logger.info('  Matched %s %s...', total, self._target)
            if total == 0:
                logger.info('  Skipping')
                continue
            elif total > self.MAX_RESULT_COUNT:
                try:
                    median = self._median_date(lower, upper)
                    logger.info('  Splitting into two smaller sections')
                    self._queue.append(Interval.between(median, upper))
                    self._queue.append(Interval.between(lower, median))
                    continue
                except TimeDifferenceTooSmallException:
                    logger.warning('  Could not be split further, mining to minimize data loss...')
            raw_results = self._convert(results)
            stored: InsertManyResult = self._store(raw_results)
            logger.info('  Stored %s %s', len(stored.inserted_ids), self._target)
        logger.info('Done!')


if __name__ == '__main__':
    parser: Final = ArgumentParser()
    parser.add_argument(
        '--token',
        required=False,
        default=environment.get("GITHUB_TOKEN"),
        help="""
        The GitHub access token to be used in mining.
        Be sure to select the 'repo' scope when generating.
        Instead of passing the token through the command line,
        you can use the `GITHUB_TOKEN` environment variable.
        You can do so at: https://github.com/settings/tokens
        """
    )
    parser.add_argument(
        '--target',
        required=True,
        choices=['commits', 'issues', 'pull-requests'],
        help="""
        GitHub Search API mining endpoint.
        Specifying 'issues' or 'pulls' will
        technically target the same endpoint,
        albeit with different settings.
        """
    )
    parser.add_argument(
        'keyword',
        help="""
        The case-insensitive keyword that will
        be targeted throughout the search.
        The script will retrieve all available
        entities that contain the keyword on
        the specified endpoint.
        """
    )
    args = parser.parse_args()
    miner = Miner(args.token, args.target, args.keyword)
    miner()
