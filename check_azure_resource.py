#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (C) 2018 Mohamed El Morabity <melmorabity@fedoraproject.com>
#
# This module is free software: you can redistribute it and/or modify it under the terms of the GNU
# General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This software is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
# even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not,
# see <http://www.gnu.org/licenses/>.


from typing import Dict, Optional, Union

from msrest.exceptions import ClientException
from msrest.service_client import ServiceClient

from msrestazure.azure_active_directory import ServicePrincipalCredentials
from msrestazure.azure_configuration import AzureConfiguration
from msrestazure.azure_exceptions import CloudError
import msrestazure.tools

from pynag import Plugins
from pynag.Plugins import simple as Plugin

from requests.exceptions import HTTPError


def _call_arm_rest_api(
    client: ServiceClient,
    path: str,
    api_version: str,
    method: str = 'GET',
    body: Optional[Dict] = None,
    query: Optional[Dict] = None,
    headers: Optional[Dict] = None,
    timeout: Optional[float] = None
) -> Union[Dict, str]:
    """Launch an Azure REST API request."""

    request = getattr(client, method.lower())(
        url=path, params=dict(query or {}, **{'api-version': api_version})
    )
    response = client.send(
        request=request, content=body,
        headers=dict(headers or {}, **{'Content-Type': 'application/json; charset=utf-8'}),
        timeout=timeout
    )

    try:
        response.raise_for_status()
    except HTTPError:
        # msrestazure.azure_exceptions.CloudError constructor provides a nice way to extract
        # Azure errors from request responses
        raise CloudError(response)

    try:
        result = response.json()
    except ValueError:
        result = response.text

    return result


class NagiosAzureResourceMonitor(Plugin):
    """Implements functionalities to grab metrics from Azure resource objects."""

    DEFAULT_AZURE_SERVICE_HOST = 'management.azure.com'
    _AZURE_METRICS_API = '2017-05-01-preview'
    _AZURE_METRICS_UNIT_SYMBOLS = {'Percent': '%', 'Bytes': 'B', 'Seconds': 's'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None
        self._metric_definitions = None
        self._metric_properties = None
        self._set_cli_options()

    def _set_cli_options(self):
        """Define command line options."""

        self.add_arg('C', 'client', 'Azure client ID')
        self.add_arg('S', 'secret', 'Azure client secret')
        self.add_arg('T', 'tenant', 'Azure tenant ID')

        self.add_arg('R', 'resource', 'Azure resource ID')
        self.add_arg('M', 'metric', 'Metric')
        self.add_arg('D', 'dimension', 'Metric dimension', required=None)
        self.add_arg('V', 'dimension-value', 'Metric dimension value', required=None)

    def activate(self):
        """Parse out all command line options and get ready to process the plugin."""
        super().activate()

        if not msrestazure.tools.is_valid_resource_id(self['resource']):
            self.parser.error('invalid resource ID')

        if bool(self['dimension']) != bool(self['dimension-value']):
            self.parser.error('--dimension and --dimension-value must be used together')

        # Set up Azure Resource Management URL
        if self['host'] is None:
            self['host'] = self.DEFAULT_AZURE_SERVICE_HOST

        # Set up timeout
        if self['timeout'] is not None:
            try:
                self['timeout'] = float(self['timeout'])
                if self['timeout'] < 0:
                    raise ValueError
            except ValueError:
                self.parser.error('Invalid timeout')

        # Authenticate to ARM
        azure_management_url = f'https://{self["host"]}'
        try:
            credentials = ServicePrincipalCredentials(
                client_id=self['client'],
                secret=self['secret'],
                tenant=self['tenant']
            )
            self._client = ServiceClient(credentials, AzureConfiguration(azure_management_url))
        except ClientException as ex:
            self.nagios_exit(Plugins.UNKNOWN, str(ex.inner_exception or ex))

        try:
            self._metric_definitions = self._get_metric_definitions()
        except CloudError as ex:
            self.nagios_exit(Plugins.UNKNOWN, ex.message)

        metric_ids = [m['name']['value'] for m in self._metric_definitions]
        if self['metric'] not in metric_ids:
            self.parser.error(
                f'Unknown metric {self["metric"]} for specified resource. '
                f'Supported metrics are: {", ".join(metric_ids)}'
            )
        self._metric_properties = self._get_metric_properties()

        dimension_ids = [d['value'] for d in self._metric_properties.get('dimensions', [])]
        if self._is_dimension_required() and self['dimension'] is None:
            self.parser.error(
                f'Dimension required for metric {self["metric"]}. '
                f'Supported dimensions are: {", ".join(dimension_ids)}'
            )
        if self['dimension'] is not None and self['dimension'] not in dimension_ids:
            self.parser.error(
                f'Unknown dimension {self["dimension"]} for metric {self["metric"]}. '
                f'Supported dimensions are: {", ".join(dimension_ids)}'
            )

    def _get_metric_definitions(self):
        """Get all available metric definitions for the Azure resource object."""

        path = f'{self["resource"]}/providers/Microsoft.Insights/metricDefinitions'
        metrics = _call_arm_rest_api(
            self._client,
            path,
            self._AZURE_METRICS_API,
            timeout=self['timeout']
        )

        return metrics['value']

    def _get_metric_properties(self):
        """Get metric properties."""

        for metric in self._metric_definitions:
            if metric['name']['value'] == self['metric']:
                return metric

        return None

    def _is_dimension_required(self):
        """Check whether an additional metric is required for a given metric ID."""

        return self._metric_properties['isDimensionRequired']

    def _get_metric_value(self):
        """Get latest metric value available for the Azure resource object."""

        query = {'metric': self['metric']}
        if self['dimension'] is not None:
            query['$filter'] = f"{self['dimension']} eq '{self['dimension-value']}'"

        path = f'{self["resource"]}/providers/Microsoft.Insights/metrics/{self["metric"]}'

        try:
            metric_values = _call_arm_rest_api(
                self._client,
                path,
                self._AZURE_METRICS_API,
                query=query,
                timeout=self['timeout']
            )
            metric_values = metric_values['value'][0]['timeseries']
        except CloudError as ex:
            self.nagios_exit(Plugins.UNKNOWN, ex.message)

        if not metric_values:
            return None

        aggregation_type = self._metric_properties['primaryAggregationType'].lower()
        # Get the latest value available
        for value in reversed(metric_values[0]['data']):
            if aggregation_type in value:
                return value[aggregation_type]

    def check_metric(self):
        """Check if the metric value is within the threshold range, and exits with status code,
        message and perfdata.
        """

        value = self._get_metric_value()
        if value is None:
            message = f'No value available for metric {self["metric"]}'
            if self['dimension'] is not None:
                message += f' and dimension {self["dimension"]}'
            self.nagios_exit(Plugins.UNKNOWN, message)

        status = Plugins.check_threshold(value, warning=self['warning'], critical=self['critical'])

        unit = self._AZURE_METRICS_UNIT_SYMBOLS.get(self._metric_properties['unit'])
        self.add_perfdata(
            self._metric_properties['name']['value'],
            value,
            uom=unit,
            warn=self['warning'],
            crit=self['critical']
        )

        self.nagios_exit(
            status,
            f'{self._metric_properties["name"]["localizedValue"]} {value} '
            f'{self._metric_properties["unit"].lower()}'
        )


if __name__ == '__main__':
    PLUGIN = NagiosAzureResourceMonitor()
    PLUGIN.activate()
    PLUGIN.check_metric()
