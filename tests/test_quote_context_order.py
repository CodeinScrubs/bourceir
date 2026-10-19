"""ترتیب عمق باید پیش از تفسیر «بهترین مظنه» معلوم باشد؛ داده فقط در آزمون است."""

import pytest

from core.quote_context import parse_context
from tests.test_legacy_quotes import observation, packet
from tests.test_quote_context import bundle


@pytest.mark.parametrize('orders',[
    '2@400@999@1001@500@3,1@600@1030@1020@100@1,',
    '2@400@999@1005@500@3,1@600@998@1001@100@1,',
    '2@400@999@1001@500@3,1@600@999@1002@100@1,',
    '2@400@999@1001@500@3,1@600@998@1001@100@1,',
    '0@0@0@1001@500@3,1@600@999@1002@100@1,',
    '2@400@999@0@0@0,1@600@998@1001@100@1,',
],ids=['better-bid-hidden','better-ask-hidden','duplicate-bid','duplicate-ask','bid-hole','ask-hole'])
def test_malformed_depth_order_cannot_hide_a_queue_or_claim_level_one_is_best(orders):
    with pytest.raises(ValueError,match='ترتیب'):
        parse_context(bundle(quote=observation(packet(orders=orders))),'آزمون')


def test_well_ordered_partial_depth_keeps_missing_and_explicit_zero_tail():
    orders='2@400@999@1001@500@3,1@600@998@1002@100@1,0@0@0@1003@50@1,'
    parsed=parse_context(bundle(quote=observation(packet(orders=orders))),'آزمون')
    assert parsed['bid_price_3']==0 and parsed['bid_price_4'] is None
    assert parsed['buy_queue_volume']==parsed['sell_queue_volume']==0
