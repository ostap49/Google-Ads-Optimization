"""GAQL for account hierarchy / metadata."""

CUSTOMER_CLIENTS = """
SELECT customer_client.id, customer_client.descriptive_name, customer_client.currency_code,
       customer_client.time_zone, customer_client.status, customer_client.manager,
       customer_client.level, customer_client.hidden, customer_client.test_account
FROM customer_client
"""
