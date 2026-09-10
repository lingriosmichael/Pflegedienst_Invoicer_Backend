import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated

from pydantic import Field


BillingMonth = Annotated[str, Field(pattern=r"^(0[1-9]|1[0-2])[1-9][0-9]{3}$")]
NonnegativeAmount = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def validate_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0[1-9]|1[0-2])[1-9][0-9]{3}", value):
        raise ValueError("Billing month must be MMYYYY")
    return value


def money(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Amount must be finite and nonnegative")
    return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_service_date(value):
    for pattern in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(str(value).strip(), pattern)
        except ValueError:
            continue
    raise ValueError("Service date must be DD.MM.YY or DD.MM.YYYY")


def month_sort_key(value):
    return datetime.strptime(value, "%m/%Y")


def patient_id_filter(value):
    variants = [value]
    try:
        variants.extend([int(value), str(value)])
    except (ValueError, TypeError):
        pass
    return {"$in": variants}
