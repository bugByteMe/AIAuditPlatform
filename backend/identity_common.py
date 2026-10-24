"""Shared identity constants and customer-scope rules."""

UNIVERSAL_CUSTOMER_ID = "universal"
UNIVERSAL_SEED_USERNAMES = ("chen.audit", "li.review")


def customer_allowed(customer_id: str, deployment_customer: str) -> bool:
  return not deployment_customer or customer_id in {deployment_customer, UNIVERSAL_CUSTOMER_ID}

