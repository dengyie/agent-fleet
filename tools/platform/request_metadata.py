"""Pure, bounded request usage and USD pricing values shared by Hub and workers.

Input/output are inclusive provider totals. Cache tokens are subsets of input;
reasoning is a subset of output. Missing detail never means zero.
"""
from decimal import Decimal, InvalidOperation
from typing import Mapping

TOKEN_FIELDS = ('input_tokens', 'cache_read_tokens', 'reasoning_tokens',
                'output_tokens', 'cache_write_tokens', 'total_tokens')
RATE_FIELDS = ('input', 'output', 'cache_read', 'cache_write')


def token_count(value):
    return value if type(value) is int and 0 <= value <= 10_000_000 else None


def decimal_amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if len(str(value)) > 64:
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0 or amount > 1_000_000 or amount.as_tuple().exponent < -12:
        return None
    return format(amount, 'f')


def validate_pricing(value):
    if (not isinstance(value, Mapping) or set(value) != {'currency', *RATE_FIELDS}
            or value.get('currency') != 'USD'):
        raise ValueError('invalid_pricing')
    rates = {key: decimal_amount(value[key]) for key in RATE_FIELDS}
    if any(rate is None for rate in rates.values()):
        raise ValueError('invalid_pricing')
    return {'currency': 'USD', **rates}


def normalized_tokens(value):
    raw = value if isinstance(value, Mapping) else {}
    usage = {key: token_count(raw.get(key)) for key in TOKEN_FIELDS}
    input_count, output_count = usage['input_tokens'], usage['output_tokens']
    # Inconsistent optional details must not create negative counts or charges.
    if input_count is not None:
        for key in ('cache_read_tokens', 'cache_write_tokens'):
            if usage[key] is not None and usage[key] > input_count:
                usage[key] = None
        if all(usage[k] is not None for k in ('cache_read_tokens', 'cache_write_tokens')):
            if usage['cache_read_tokens'] + usage['cache_write_tokens'] > input_count:
                usage['cache_read_tokens'] = usage['cache_write_tokens'] = None
    if output_count is not None and usage['reasoning_tokens'] is not None and usage['reasoning_tokens'] > output_count:
        usage['reasoning_tokens'] = None
    if input_count is not None and output_count is not None:
        usage['total_tokens'] = input_count + output_count
    return usage


def request_cost(usage, *, reported=None, pricing=None):
    amount = decimal_amount(reported)
    if amount is not None:
        return {'amount': amount, 'currency': 'USD', 'source': 'provider'}
    if pricing is None:
        return None
    try:
        rates = validate_pricing(pricing)
    except ValueError:
        return None
    inp, out = usage.get('input_tokens'), usage.get('output_tokens')
    if inp is None or out is None:
        return None
    quantities = {'input': inp, 'output': out}
    unspecified = False
    for category, field in (('cache_read', 'cache_read_tokens'), ('cache_write', 'cache_write_tokens')):
        count = usage.get(field)
        if count is None:
            # When rates are identical, an unknown split cannot change cost.
            # Keep those tokens in input; do not manufacture a zero cache count.
            if Decimal(rates[category]) != Decimal(rates['input']):
                return None
            unspecified = True
        else:
            quantities['input'] -= count
            quantities[category] = count
    if quantities['input'] < 0:
        return None
    items = [{'category': k, 'tokens': quantities[k], 'rate_per_million': rates[k],
              'amount': format(Decimal(quantities[k]) * Decimal(rates[k]) / 1_000_000, 'f')}
             for k in RATE_FIELDS if k in quantities]
    if unspecified:
        items[0]['includes_unspecified_cache'] = True
    return {'amount': format(sum(Decimal(x['amount']) for x in items), 'f'),
            'currency': 'USD', 'source': 'configured', 'items': items}
