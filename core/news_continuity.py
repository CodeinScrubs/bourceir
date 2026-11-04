"""Evidence for a bounded scan of a mutable, newest-first public news feed.

A satisfied window is conditional on the observed pagination and stable head.
It does not prove that the publisher exposed every notice or never backdated one.
"""

from datetime import timedelta

from core.codal_news import verify_page_evidence
from core.news_backoff import SERVICE_VERSION, checked_backoff, deferred, make_backoff
from core.opportunity_radar import digest, stamp

VERSION = 'news-window-v4'
LEGACY_VERSION = 'news-window-v3'
ORDER_DIAGNOSTIC_LIMIT = 20


def next_window_start(previous, *, registered_at, overlap_seconds=7200):
    """An incomplete scan must retain its lower boundary across restarts."""
    if type(overlap_seconds) is not int or overlap_seconds < 0:
        raise ValueError('A nonnegative overlap is required')
    if previous:
        coverage = previous.get('news_coverage', {})
        if coverage.get('window_scan_satisfied') is True and coverage.get('checkpoint_through'):
            return (stamp(coverage['checkpoint_through'])-timedelta(seconds=overlap_seconds)).isoformat()
        if coverage.get('window_start'):
            return stamp(coverage['window_start']).isoformat()
    return (stamp(registered_at)-timedelta(days=1)).isoformat()


class NewsWindow:
    def __init__(self, *, since, requested_at, initial_rate_limit_state=None, version=VERSION):
        if version not in (VERSION, LEGACY_VERSION):
            raise ValueError('Unsupported news window version')
        self.version = version
        self.since, self.started = stamp(since), stamp(requested_at)
        if self.since > self.started:
            raise ValueError('News window starts after the observation')
        self.initial_rate_limit_state = checked_backoff(initial_rate_limit_state)
        if (self.version == LEGACY_VERSION and self.initial_rate_limit_state is not None
                and self.initial_rate_limit_state['version'] == SERVICE_VERSION):
            raise ValueError('Legacy news window cannot contain a newer service cooldown')
        self.rate_limit_deferred = deferred(self.initial_rate_limit_state, at=requested_at)
        self.rate_limit_state = self.initial_rate_limit_state if self.rate_limit_deferred else None
        self.pages, self.letters, self.errors = [], [], []
        self.seen = set()
        self.last_time = None
        self.last_context = None
        self.order_violations = []
        self.order_violation_count = 0
        self.oldest = self.newest = None
        self.boundary_reached = False
        self.reached_end = False
        self.failed_page = None
        self.invalid_rows = 0

    def add(self, page):
        if self.rate_limit_deferred:
            raise ValueError('A deferred news scan cannot contain requested pages')
        number = len(self.pages)+1
        if page.get('requested_page') != number:
            raise ValueError('Consecutive news pages are required')
        native = verify_page_evidence(page)
        if native and (stamp(page['requested_at']) < self.started or self.pages
                and stamp(page['requested_at']) < stamp(self.pages[-1]['received_at'])):
            raise ValueError('Native news page request precedes its scan or prior response')
        self.pages.append(page)
        backoff = make_backoff(page, include_service_unavailable=self.version == VERSION)
        if backoff is not None:
            self.rate_limit_state = backoff
        if page.get('status') not in ('observed', 'partial'):
            self.failed_page = number
            self.errors.append('page_unavailable')
            return
        invalid = page.get('invalid_rows', 0)
        self.invalid_rows += invalid
        if invalid:
            self.errors.append('invalid_source_rows')
        letters = page['letters']
        if not letters and page.get('raw_count') == 0:
            self.reached_end = self.boundary_reached = True
        for position, letter in enumerate(letters, 1):
            key = digest(letter)
            if key in self.seen:
                continue
            self.seen.add(key)
            self.letters.append(letter)
            try:
                published = stamp(letter.get('published_at'))
                received = stamp(page['received_at'])
                if published > received:
                    raise ValueError('Future publication time')
            except ValueError:
                self.errors.append('invalid_publication_time')
                continue
            context = dict(page=number,row=position,tracing_no=letter.get('tracing_no'),
                           symbol_fa=letter.get('symbol_fa'),published_at=letter.get('published_at'),
                           letter_sha256=key)
            if self.last_time is not None and published > self.last_time:
                self.errors.append('publication_order_changed')
                self.order_violation_count += 1
                if len(self.order_violations) < ORDER_DIAGNOSTIC_LIMIT:
                    self.order_violations.append(dict(previous_valid=self.last_context,current=context))
            self.last_time = published
            self.last_context = context
            self.oldest = min(self.oldest, published) if self.oldest else published
            self.newest = max(self.newest, published) if self.newest else published
        # Equality is insufficient: more notices with the same second may follow.
        if letters and self.last_time is not None and self.last_time < self.since:
            self.boundary_reached = True

    @staticmethod
    def head_identity(page):
        verify_page_evidence(page)
        if page.get('status') != 'observed' or page.get('invalid_rows'):
            return None
        # Changes outside the sampled head remain an explicit limitation.
        return digest(page['letters'])

    def result(self, *, head_recheck=None, stop_reason):
        if self.rate_limit_deferred and (stop_reason != 'rate_limit_cooldown' or head_recheck is not None):
            raise ValueError('Deferred news must retain its explicit reason without a head request')
        head = self.head_identity(self.pages[0]) if self.pages else None
        stable = head is not None and head == self.head_identity(head_recheck or {})
        if head_recheck and verify_page_evidence(head_recheck) and (not self.pages
                or stamp(head_recheck['requested_at']) < stamp(self.pages[-1]['received_at'])):
            raise ValueError('Native head recheck precedes the retained scan')
        rate_limit_state = make_backoff(head_recheck, include_service_unavailable=self.version == VERSION) or self.rate_limit_state
        errors = sorted(set(self.errors+(['rate_limit_cooldown'] if self.rate_limit_deferred else [])))
        satisfied = bool(self.boundary_reached and not errors and stable)
        if self.boundary_reached and not self.errors and not stable and stop_reason != 'native_body_budget':
            stop_reason = ('head_changed' if self.head_identity(head_recheck or {}) is not None
                           else 'head_recheck_unavailable')
        return dict(version=self.version, letters=self.letters, pages=self.pages, head_recheck=head_recheck,
            window_start=self.since.isoformat(), requested_at=self.started.isoformat(),
            pages_fetched=sum(p.get('status') in ('observed', 'partial') for p in self.pages),
            failed_page=self.failed_page, page_limit_reached=stop_reason == 'page_limit',
            reached_end=self.reached_end, stop_reason=stop_reason, errors=errors,
            initial_rate_limit_state=self.initial_rate_limit_state,rate_limit_state=rate_limit_state,
            rate_limit_deferred=self.rate_limit_deferred,
            invalid_rows=self.invalid_rows, boundary_reached=self.boundary_reached,
            head_recheck_stable=stable, window_scan_satisfied=satisfied,
            checkpoint_through=self.started.isoformat() if satisfied else None,
            oldest_published_at=self.oldest.isoformat() if self.oldest else None,
            newest_published_at=self.newest.isoformat() if self.newest else None,
            publication_order_violation_count=self.order_violation_count,
            publication_order_violations=self.order_violations,
            publication_order_diagnostics_truncated=self.order_violation_count>len(self.order_violations),
            complete_coverage_verified=False, publisher_snapshot_atomic=False)
