from __future__ import annotations

from typing import Dict

import requests
from . import types


def schema_defs_from_url(schemas_url) -> Dict[str, types.DatasetSchema]:
    """Fetch all schema definitions from a remote file.
    The URL could be ``https://schemas.data.amsterdam.nl/datasets/``
    """
    schema_lookup = {}
    with requests.Session() as connection:

        # Fetch complete lookup
        response = connection.get(schemas_url)
        response.raise_for_status()
        for schema_dir_info in response.json():
            # Fetch folder data of datasets
            schema_dir_name = schema_dir_info["name"]
            response = connection.get(f"{schemas_url}{schema_dir_name}/")
            response.raise_for_status()

            for schema_file_info in response.json():
                # Fetch each schema from the folder
                schema_name = schema_file_info["name"]
                response = connection.get(f"{schemas_url}{schema_dir_name}/{schema_name}")
                response.raise_for_status()
                schema_lookup[schema_name] = types.DatasetSchema.from_dict(response.json())

    return schema_lookup


def schema_def_from_url(schemas_url, schema_name):
    return schema_defs_from_url(schemas_url)[schema_name]
