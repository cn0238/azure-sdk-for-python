# ------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# -------------------------------------------------------------------------
from logging import getLogger
import json
import time
import random
from dataclasses import dataclass
from typing import Tuple, Union, Dict, List, Any, Optional, Mapping
from typing_extensions import Self
from azure.core import MatchConditions
from azure.core.tracing.decorator import distributed_trace
from azure.core.exceptions import HttpResponseError
from azure.core.credentials import TokenCredential
from azure.appconfiguration import (  # type:ignore # pylint:disable=no-name-in-module
    ConfigurationSetting,
    AzureAppConfigurationClient,
    FeatureFlagConfigurationSetting,
)
from ._models import SettingSelector
from ._constants import (
    FEATURE_FLAG_PREFIX,
    PERCENTAGE_FILTER_NAMES,
    TIME_WINDOW_FILTER_NAMES,
    TARGETING_FILTER_NAMES,
    CUSTOM_FILTER_KEY,
    PERCENTAGE_FILTER_KEY,
    TIME_WINDOW_FILTER_KEY,
    TARGETING_FILTER_KEY,
)
from ._discovery import find_auto_failover_endpoints

FALLBACK_CLIENT_REFRESH_EXPIRED_INTERVAL = 3600  # 1 hour in seconds
MINIMAL_CLIENT_REFRESH_INTERVAL = 30  # 30 seconds


@dataclass
class _ConfigurationClientWrapper:
    endpoint: str
    _client: AzureAppConfigurationClient
    backoff_end_time: float = 0
    failed_attempts: int = 0
    LOGGER = getLogger(__name__)

    @classmethod
    def from_credential(
        cls,
        endpoint: str,
        credential: TokenCredential,
        user_agent: str,
        retry_total: int,
        retry_backoff_max: int,
        **kwargs
    ) -> Self:
        """
        Creates a new instance of the _ConfigurationClientWrapper class, using the provided credential to authenticate
        requests.

        :param str endpoint: The endpoint of the App Configuration store
        :param TokenCredential credential: The credential to use for authentication
        :param str user_agent: The user agent string to use for the request
        :param int retry_total: The total number of retries to allow for requests
        :param int retry_backoff_max: The maximum backoff time for retries
        :return: A new instance of the _ConfigurationClientWrapper class
        :rtype: _ConfigurationClientWrapper
        """
        return cls(
            endpoint,
            AzureAppConfigurationClient(
                endpoint,
                credential,
                user_agent=user_agent,
                retry_total=retry_total,
                retry_backoff_max=retry_backoff_max,
                **kwargs
            ),
        )

    @classmethod
    def from_connection_string(
        cls, endpoint: str, connection_string: str, user_agent: str, retry_total: int, retry_backoff_max: int, **kwargs
    ) -> Self:
        """
        Creates a new instance of the _ConfigurationClientWrapper class, using the provided connection string to
        authenticate requests.

        :param str endpoint: The endpoint of the App Configuration store
        :param str connection_string: The connection string to use for authentication
        :param str user_agent: The user agent string to use for the request
        :param int retry_total: The total number of retries to allow for requests
        :param int retry_backoff_max: The maximum backoff time for retries
        :return: A new instance of the _ConfigurationClientWrapper class
        :rtype: _ConfigurationClientWrapper
        """
        return cls(
            endpoint,
            AzureAppConfigurationClient.from_connection_string(
                connection_string,
                user_agent=user_agent,
                retry_total=retry_total,
                retry_backoff_max=retry_backoff_max,
                **kwargs
            ),
        )

    def _check_configuration_setting(
        self, key: str, label: str, etag: Optional[str], headers: Dict[str, str], **kwargs
    ) -> Tuple[bool, Union[ConfigurationSetting, None]]:
        """
        Checks if the configuration setting have been updated since the last refresh.

        :param str key: key to check for chances
        :param str label: label to check for changes
        :param str etag: etag to check for changes
        :param Mapping[str, str] headers: headers to use for the request
        :return: A tuple with the first item being true/false if a change is detected. The second item is the updated
        value if a change was detected.
        :rtype: Tuple[bool, Union[ConfigurationSetting, None]]
        """
        try:
            updated_sentinel = self._client.get_configuration_setting(  # type: ignore
                key=key, label=label, etag=etag, match_condition=MatchConditions.IfModified, headers=headers, **kwargs
            )
            if updated_sentinel is not None:
                self.LOGGER.debug(
                    "Refresh all triggered by key: %s label %s.",
                    key,
                    label,
                )
                return True, updated_sentinel
        except HttpResponseError as e:
            if e.status_code == 404:
                if etag is not None:
                    # If the sentinel is not found, it means the key/label was deleted, so we should refresh
                    self.LOGGER.debug("Refresh all triggered by key: %s label %s.", key, label)
                    return True, None
            else:
                raise e
        return False, None

    @distributed_trace
    def load_configuration_settings(
        self, selects: List[SettingSelector], refresh_on: Dict[Tuple[str, str], str], **kwargs
    ) -> Tuple[List[ConfigurationSetting], Dict[Tuple[str, str], str]]:
        configuration_settings = []
        sentinel_keys = kwargs.pop("sentinel_keys", refresh_on)
        for select in selects:
            configurations = self._client.list_configuration_settings(
                key_filter=select.key_filter, label_filter=select.label_filter, **kwargs
            )
            for config in configurations:
                if isinstance(config, FeatureFlagConfigurationSetting):
                    # Feature flags are ignored when loaded by Selects, as they are selected from
                    # `feature_flag_selectors`
                    pass
                configuration_settings.append(config)
                # Every time we run load_all, we should update the etag of our refresh sentinels
                # so they stay up-to-date.
                # Sentinel keys will have unprocessed key names, so we need to use the original key.
                if (config.key, config.label) in refresh_on:
                    sentinel_keys[(config.key, config.label)] = config.etag
        return configuration_settings, sentinel_keys

    @distributed_trace
    def load_feature_flags(
        self, feature_flag_selectors: List[SettingSelector], feature_flag_refresh_enabled: bool, **kwargs
    ) -> Tuple[List[FeatureFlagConfigurationSetting], Dict[Tuple[str, str], str], Dict[str, bool]]:
        feature_flag_sentinel_keys = {}
        loaded_feature_flags = []
        # Needs to be removed unknown keyword argument for list_configuration_settings
        kwargs.pop("sentinel_keys", None)
        filters_used = {}
        for select in feature_flag_selectors:
            feature_flags = self._client.list_configuration_settings(
                key_filter=FEATURE_FLAG_PREFIX + select.key_filter, label_filter=select.label_filter, **kwargs
            )
            for feature_flag in feature_flags:
                loaded_feature_flags.append(json.loads(feature_flag.value))

                if feature_flag_refresh_enabled:
                    feature_flag_sentinel_keys[(feature_flag.key, feature_flag.label)] = feature_flag.etag
                if feature_flag.filters:
                    for filter in feature_flag.filters:
                        if filter.get("name") in PERCENTAGE_FILTER_NAMES:
                            filters_used[PERCENTAGE_FILTER_KEY] = True
                        elif filter.get("name") in TIME_WINDOW_FILTER_NAMES:
                            filters_used[TIME_WINDOW_FILTER_KEY] = True
                        elif filter.get("name") in TARGETING_FILTER_NAMES:
                            filters_used[TARGETING_FILTER_KEY] = True
                        else:
                            filters_used[CUSTOM_FILTER_KEY] = True
        return loaded_feature_flags, feature_flag_sentinel_keys, filters_used

    @distributed_trace
    def refresh_configuration_settings(
        self, selects: List[SettingSelector], refresh_on: Dict[Tuple[str, str], str], headers: Dict[str, str], **kwargs
    ) -> Tuple[bool, Dict[Tuple[str, str], str], List[Any]]:
        """
        Gets the refreshed configuration settings if they have changed.

        :param List[SettingSelector] selects: The selectors to use to load configuration settings
        :param List[SettingSelector] refresh_on: The configuration settings to check for changes
        :param Mapping[str, str] headers: The headers to use for the request

        :return: A tuple with the first item being true/false if a change is detected. The second item is the updated
        value of the configuration sentinel keys. The third item is the updated configuration settings.
        :rtype: Tuple[bool, Union[Dict[Tuple[str, str], str], None], Union[List[ConfigurationSetting], None]]
        """
        need_refresh = False
        updated_sentinel_keys = dict(refresh_on)
        for (key, label), etag in updated_sentinel_keys.items():
            changed, updated_sentinel = self._check_configuration_setting(
                key=key, label=label, etag=etag, headers=headers, **kwargs
            )
            if changed:
                need_refresh = True
            if updated_sentinel is not None:
                updated_sentinel_keys[(key, label)] = updated_sentinel.etag
        # Need to only update once, no matter how many sentinels are updated
        if need_refresh:
            configuration_settings, sentinel_keys = self.load_configuration_settings(selects, refresh_on, **kwargs)
            return True, sentinel_keys, configuration_settings
        return False, refresh_on, []

    @distributed_trace
    def refresh_feature_flags(
        self,
        refresh_on: Mapping[Tuple[str, str], Optional[str]],
        feature_flag_selectors: List[SettingSelector],
        headers: Dict[str, str],
        **kwargs
    ) -> Tuple[bool, Optional[Dict[Tuple[str, str], str]], Optional[List[Any]], Dict[str, bool]]:
        """
        Gets the refreshed feature flags if they have changed.

        :param Mapping[Tuple[str, str], Optional[str]] refresh_on: The feature flags to check for changes
        :param List[SettingSelector] feature_flag_selectors: The selectors to use to load feature flags
        :param Mapping[str, str] headers: The headers to use for the request

        :return: A tuple with the first item being true/false if a change is detected. The second item is the updated
        value of the feature flag sentinel keys. The third item is the updated feature flags.
        :rtype: Tuple[bool, Union[Dict[Tuple[str, str], str], None], Union[List[Dict[str, str]], None, Dict[str, bool]]
        """
        feature_flag_sentinel_keys: Mapping[Tuple[str, str], Optional[str]] = dict(refresh_on)
        for (key, label), etag in feature_flag_sentinel_keys.items():
            changed = self._check_configuration_setting(key=key, label=label, etag=etag, headers=headers, **kwargs)
            if changed:
                feature_flags, feature_flag_sentinel_keys, filters_used = self.load_feature_flags(
                    feature_flag_selectors, True, **kwargs
                )
                return True, feature_flag_sentinel_keys, feature_flags, filters_used
        return False, None, None, {}

    @distributed_trace
    def get_configuration_setting(self, key: str, label: str, **kwargs) -> ConfigurationSetting:
        """
        Gets a configuration setting from the replica client.

        :param str key: The key of the configuration setting
        :param str label: The label of the configuration setting
        :return: The configuration setting
        :rtype: ConfigurationSetting
        """
        return self._client.get_configuration_setting(key=key, label=label, **kwargs)

    def is_active(self) -> bool:
        """
        Checks if the client is active and can be used.

        :return: True if the client is active, False otherwise
        :rtype: bool
        """
        return self.backoff_end_time <= (time.time() * 1000)

    def close(self) -> None:
        """
        Closes the connection to Azure App Configuration.
        """
        self._client.close()

    def __enter__(self):
        self._client.__enter__()
        return self

    def __exit__(self, *args):
        self._client.__exit__(*args)


class ConfigurationClientManager:  # pylint:disable=too-many-instance-attributes
    def __init__(
        self,
        connection_string: Optional[str],
        endpoint: str,
        credential: Optional["TokenCredential"],
        user_agent: str,
        retry_total,
        retry_backoff_max,
        replica_discovery_enabled,
        min_backoff_sec,
        max_backoff_sec,
        **kwargs
    ):
        self._replica_clients = []
        self._original_endpoint = endpoint
        self._original_connection_string = connection_string
        self._credential = credential
        self._user_agent = user_agent
        self._retry_total = retry_total
        self._retry_backoff_max = retry_backoff_max
        self._replica_discovery_enabled = replica_discovery_enabled
        self._next_update_time = time.time() + MINIMAL_CLIENT_REFRESH_INTERVAL
        self._args = dict(kwargs)
        self._min_backoff_sec = min_backoff_sec
        self._max_backoff_sec = max_backoff_sec

        failover_endpoints = find_auto_failover_endpoints(endpoint, replica_discovery_enabled)
        if connection_string and endpoint:
            self._replica_clients.append(
                _ConfigurationClientWrapper.from_connection_string(
                    endpoint, connection_string, user_agent, retry_total, retry_backoff_max, **self._args
                )
            )
            for failover_endpoint in failover_endpoints:
                failover_connection_string = connection_string.replace(endpoint, failover_endpoint)
                self._replica_clients.append(
                    _ConfigurationClientWrapper.from_connection_string(
                        failover_endpoint,
                        failover_connection_string,
                        user_agent,
                        retry_total,
                        retry_backoff_max,
                        **self._args
                    )
                )
            return
        if endpoint and credential:
            self._replica_clients.append(
                _ConfigurationClientWrapper.from_credential(
                    endpoint, credential, user_agent, retry_total, retry_backoff_max, **self._args
                )
            )
            for failover_endpoint in failover_endpoints:
                self._replica_clients.append(
                    _ConfigurationClientWrapper.from_credential(
                        failover_endpoint, credential, user_agent, retry_total, retry_backoff_max, **self._args
                    )
                )
            return
        raise ValueError("Please pass either endpoint and credential, or a connection string with a value.")

    def refresh_clients(self):
        if not self._replica_discovery_enabled:
            return
        if self._next_update_time > time.time():
            return
        failover_endpoints = find_auto_failover_endpoints(self._original_endpoint, self._replica_discovery_enabled)

        if failover_endpoints is None:
            # SRV record not found, so we should refresh after a longer interval
            self._next_update_time = time.time() + FALLBACK_CLIENT_REFRESH_EXPIRED_INTERVAL
            return

        if len(failover_endpoints) == 0:
            # No failover endpoints in SRV record.
            self._next_update_time = time.time() + MINIMAL_CLIENT_REFRESH_INTERVAL
            return

        new_clients = [self._replica_clients[0]]  # Keep the original client
        for failover_endpoint in failover_endpoints:
            found_client = False
            for client in self._replica_clients:
                if client.endpoint == failover_endpoint:
                    new_clients.append(client)
                    found_client = True
                    break
            if not found_client:
                if self._original_connection_string:
                    failover_connection_string = self._original_connection_string.replace(
                        self._original_endpoint, failover_endpoint
                    )
                    new_clients.append(
                        _ConfigurationClientWrapper.from_connection_string(
                            failover_endpoint,
                            failover_connection_string,
                            self._user_agent,
                            self._retry_total,
                            self._retry_backoff_max,
                            **self._args
                        )
                    )
                else:
                    new_clients.append(
                        _ConfigurationClientWrapper.from_credential(
                            failover_endpoint,
                            self._credential,
                            self._user_agent,
                            self._retry_total,
                            self._retry_backoff_max,
                            **self._args
                        )
                    )
        self._replica_clients = new_clients
        self._next_update_time = time.time() + MINIMAL_CLIENT_REFRESH_INTERVAL

    def get_active_clients(self):
        active_clients = []
        for client in self._replica_clients:
            if client.is_active():
                active_clients.append(client)
        return active_clients

    def backoff(self, client: _ConfigurationClientWrapper):
        client.failed_attempts += 1
        backoff_time = self._calculate_backoff(client.failed_attempts)
        client.backoff_end_time = (time.time() * 1000) + backoff_time

    def get_client_count(self) -> int:
        return len(self._replica_clients)

    def _calculate_backoff(self, attempts: int) -> float:
        max_attempts = 63
        ms_per_second = 1000  # 1 Second in milliseconds

        min_backoff_milliseconds = self._min_backoff_sec * ms_per_second
        max_backoff_milliseconds = self._max_backoff_sec * ms_per_second

        if self._max_backoff_sec <= self._min_backoff_sec:
            return min_backoff_milliseconds

        calculated_milliseconds = max(1, min_backoff_milliseconds) * (1 << min(attempts, max_attempts))

        if calculated_milliseconds > max_backoff_milliseconds or calculated_milliseconds <= 0:
            calculated_milliseconds = max_backoff_milliseconds

        return min_backoff_milliseconds + (
            random.uniform(0.0, 1.0) * (calculated_milliseconds - min_backoff_milliseconds)
        )

    def __eq__(self, other):
        if len(self._replica_clients) != len(other._replica_clients):
            return False
        for i in range(len(self._replica_clients)):  # pylint:disable=consider-using-enumerate
            if self._replica_clients[i] != other._replica_clients[i]:
                return False
        return True

    def close(self):
        for client in self._replica_clients:
            client.close()

    def __enter__(self):
        for client in self._replica_clients:
            client.__enter__()
        return self

    def __exit__(self, *args):
        for client in self._replica_clients:
            client.__exit__(*args)
