"""保存済み決算書から発行済み株数の候補を読む。

表紙の最近の株数や発行可能株数を、過去の四半期末の分母に流用しない。
これは対応する株式種類の最終確認を代替するものではない。
"""
import io
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from .cohort import PERIOD

XBRLI = 'http://www.xbrl.org/2003/instance'
IX = {'http://www.xbrl.org/2013/inlineXBRL', 'http://www.xbrl.org/2008/inlineXBRL'}
TAGS = {'CommonStockSharesOutstanding', 'EntityCommonStockSharesOutstanding'}


def split_tag(tag):
    return tag[1:].split('}', 1) if tag.startswith('{') else ('', tag)


def parse_document(text):
    # SECの全文には、本文の外側に独自のXBRL包装が付くことがある。
    text = text.strip()
    if text.startswith('<XBRL>') and text.endswith('</XBRL>'):
        text = text[6:-7].strip()
    if '<!ENTITY' in text:
        raise ValueError('entity_declarations_not_supported')
    namespaces = defaultdict(set)
    parser = ET.iterparse(io.StringIO(text), events=('start-ns', 'end'))
    for event, item in parser:
        if event == 'start-ns':
            namespaces[item[0]].add(item[1])
    return parser.root, namespaces


def resolve_qname(value, namespaces):
    prefix, separator, name = value.partition(':')
    uris = namespaces.get(prefix if separator else '', set())
    if len(uris) != 1:
        raise ValueError('missing_or_rebound_namespace')
    return next(iter(uris)), name if separator else prefix


def share_number(element, namespaces):
    """見た目の数字に桁倍率を適用する。小数精度は倍率とは別に記録する。"""
    if element.get('{http://www.w3.org/2001/XMLSchema-instance}nil') in {'true', '1'}:
        raise ValueError('nil_share_fact')
    for child in element.iter():
        if child is not element and split_tag(child.tag)[0] in IX:
            raise ValueError('nested_inline_numeric_content_requires_review')
    raw = ''.join(element.itertext()).strip()
    transformation = element.get('format')
    if transformation:
        uri, name = resolve_qname(transformation, namespaces)
        if (not re.fullmatch(r'https?://www\.xbrl\.org/inlineXBRL/transformation/\d{4}-\d{2}-\d{2}', uri)
                or name not in {'num-dot-decimal', 'numdotdecimal', 'numcommadot'}):
            raise ValueError('unsupported_numeric_transformation')
        if not re.fullmatch(r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?', raw):
            raise ValueError('ambiguous_share_number')
        raw = raw.replace(',', '')
    elif not re.fullmatch(r'\d+(?:\.\d+)?', raw):
        raise ValueError('invalid_unformatted_share_number')
    try:
        scale = int(element.get('scale', '0'))
        if not -12 <= scale <= 12 or element.get('sign') not in {None, ''}:
            raise ValueError('unsupported_share_scale_or_sign')
        value = Decimal(raw) * Decimal(10) ** scale
        if not value.is_finite() or value <= 0 or value != value.to_integral_value():
            raise ValueError('nonpositive_or_fractional_share_count')
        decimals = element.get('decimals')
        if decimals != 'INF' and (decimals is None or not -12 <= int(decimals) <= 12):
            raise ValueError('unknown_share_precision')
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError('invalid_share_number') from exc
    return {'shares': int(value), 'displayed_number': raw, 'scale': scale,
            'decimals': decimals,
            'rounded_to_shares': 1 if decimals == 'INF' or int(decimals) >= 0 else 10 ** -int(decimals)}


def outstanding_candidates(text, cik, period=PERIOD):
    root, namespaces = parse_document(text)
    contexts, units = {}, {}
    for element in root.iter():
        if element.tag not in {f'{{{XBRLI}}}context', f'{{{XBRLI}}}unit'}:
            continue
        destination = contexts if element.tag.endswith('context') else units
        identifier = element.get('id')
        if not identifier or identifier in destination:
            raise ValueError('duplicate_or_missing_context_or_unit_id')
        destination[identifier] = element
    facts, rejected = [], Counter()
    for element in root.iter():
        namespace, kind = split_tag(element.tag)
        if namespace not in IX or kind != 'nonFraction':
            continue
        name = element.get('name', '')
        if name.split(':')[-1] not in TAGS:
            continue
        try:
            taxonomy, tag = resolve_qname(name, namespaces)
            expected = (r'https?://fasb\.org/us-gaap/\d{4}' if tag == 'CommonStockSharesOutstanding'
                        else r'https?://xbrl\.sec\.gov/dei/\d{4}')
            if not re.fullmatch(expected, taxonomy):
                raise ValueError('nonstandard_outstanding_tag')
            context = contexts.get(element.get('contextRef'))
            if context is None:
                raise ValueError('missing_context')
            identifiers = list(context.iter(f'{{{XBRLI}}}identifier'))
            if (len(identifiers) != 1 or (identifiers[0].text or '').strip().zfill(10) != cik
                    or identifiers[0].get('scheme') not in {'http://www.sec.gov/CIK', 'https://www.sec.gov/CIK'}):
                raise ValueError('context_issuer_mismatch')
            instants = list(context.iter(f'{{{XBRLI}}}instant'))
            if len(instants) != 1 or list(context.iter(f'{{{XBRLI}}}startDate')):
                raise ValueError('not_instant_share_fact')
            end = (instants[0].text or '').strip()
            if end != period:
                raise ValueError('not_target_period')
            # 種類別・子会社別の数字を、全体の分母として足し合わせない。
            if any(split_tag(e.tag)[1] in {'segment', 'scenario'} and len(e) for e in context.iter()):
                raise ValueError('dimensional_share_fact_requires_class_review')
            unit = units.get(element.get('unitRef'))
            measures = list(unit.iter(f'{{{XBRLI}}}measure')) if unit is not None else []
            if (len(measures) != 1 or unit is None or list(unit.iter(f'{{{XBRLI}}}divide'))
                    or resolve_qname((measures[0].text or '').strip(), namespaces) != (XBRLI, 'shares')):
                raise ValueError('not_share_units')
            facts.append({**share_number(element, namespaces), 'tag': tag,
                          'context_id': element.get('contextRef'), 'fact_id': element.get('id'), 'end': end})
        except ValueError as exc:
            rejected[str(exc)] += 1
    values = {f['shares'] for f in facts}
    # 同じ期末に複数の値があれば、都合のよい方を選択しない。
    status = 'exact_period_numeric_candidate' if len(values) == 1 else ('conflicting_period_facts' if values else 'no_exact_period_fact')
    return {'status': status, 'shares': next(iter(values)) if len(values) == 1 else None,
            'period': period, 'facts': facts, 'rejected_fact_reasons': dict(rejected),
            'security_class_verified': False, 'denominator_certified': False}
