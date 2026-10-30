import logging

import pandas as pd
from google.cloud import bigquery

logger = logging.getLogger("opsfleet-assignment")


class BigQueryRunner:
    """
    A lean BigQuery client for executing SQL queries and returning 
    DataFrame results."""

    def __init__(self, project_id: str | None = None,
                 dataset_id: str | None = \
                     "bigquery-public-data.thelook_ecommerce") -> None:
        """Initialize BigQuery client.
    
        Args:
            project_id: Google Cloud project ID. If None, uses default creds.
            dataset_id: BigQuery dataset ID. If None, uses default dataset.
        """
        logger.info("Initializing BigQuery client")
        try:
            self.client = bigquery.Client(project=project_id)
            self.dataset_id = dataset_id
            logger.info("BigQuery client initialized for dataset: "
                        f"{self.dataset_id}")
        except Exception as e:
            logger.error(f"Failed to initialize BigQuery client: {e}")
            raise

    def execute_query(self, sql_query: str) -> pd.DataFrame:
        """Execute a SQL query and return results as a DataFrame.

        Args:
            sql_query: The SQL query to execute.

        Returns:
            DataFrame containing the query results.
    
        Raises:
            Exception: If query execution fails.
        """
        try:
            logger.info("Executing BigQuery query")
            query_job = self.client.query(sql_query)
            df = query_job.result().to_dataframe()
            logger.info(f"Query completed successfully, returned {len(df)} rows")
            return df
        except Exception as e:
            logger.error(f"BigQuery execution failed: {e}")
            raise

    def get_table_schema(self, table_name: str) -> list[dict[str, Any]]:
        """Get schema information for a specific table.

        Args:
            table_name: Name of the table (orders, order_items, products, users).

        Returns:
            List of dictionaries containing column information.
        """
        try:
            table_ref = f"{self.dataset_id}.{table_name}"
            table = self.client.get_table(table_ref)
            schema_info = []
            for field in table.schema:
                schema_info.append({
                    "name": field.name,
                    "type": field.field_type,
                    "mode": field.mode,
                    "description": field.description or ""
                })
            logger.info(f"Retrieved schema for table {table_name}")
            return schema_info
        except Exception as e:
            logger.error(f"Failed to get schema for table {table_name}: {e}")
            raise
