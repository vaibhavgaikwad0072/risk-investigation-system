
import './App.css'
import { useState, type FormEvent } from 'react'
import {
  AssessmentApiError,
  createAssessment,
  type AssessmentRequest,
  type AssessmentResponse,
  type RiskAssessment,
} from './api/riskAssessments'

type KnownRiskBand = 'low' | 'medium' | 'high' | 'critical'
type AssessmentSort = 'score_desc' | 'timestamp_desc' | 'timestamp_asc' | 'amount_desc' | 'amount_asc'

const riskBands: KnownRiskBand[] = ['low', 'medium', 'high', 'critical']

const transactionTypeLabels: Record<string, string> = {
  transfer: 'Transfer',
  card_payment: 'Card payment',
  cash_withdrawal: 'Cash withdrawal',
  deposit: 'Deposit',
}

function getRiskBandCounts(assessments: RiskAssessment[]) {
  const counts: Record<KnownRiskBand, number> = {
    low: 0,
    medium: 0,
    high: 0,
    critical: 0,
  }
  let other = 0

  for (const assessment of assessments) {
    if (riskBands.includes(assessment.risk_band as KnownRiskBand)) {
      counts[assessment.risk_band as KnownRiskBand] += 1
    } else {
      other += 1
    }
  }

  return { counts, other }
}

function formatAmount(amountMinorUnits: number, currency: string) {
  try {
    const formatter = new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency,
    })
    const fractionDigits = formatter.resolvedOptions().maximumFractionDigits ?? 2
    return formatter.format(amountMinorUnits / 10 ** fractionDigits)
  } catch {
    return `${amountMinorUnits.toLocaleString()} minor units ${currency}`
  }
}

function formatTimestamp(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Invalid timestamp'

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

function formatRiskBand(value: string) {
  return value.length > 0
    ? value.charAt(0).toUpperCase() + value.slice(1).toLowerCase()
    : 'Unknown'
}

function matchesAssessmentFilters(
  assessment: RiskAssessment,
  search: string,
  riskBand: string,
  transactionType: string,
) {
  const normalizedSearch = search.trim().toLocaleLowerCase()
  const matchesSearch =
    !normalizedSearch ||
    [
      assessment.transaction_ref,
      assessment.source_account_ref,
      assessment.counterparty_account_ref ?? '',
    ].some((value) => value.toLocaleLowerCase().includes(normalizedSearch))

  return (
    matchesSearch &&
    (riskBand === 'all' || assessment.risk_band.toLocaleLowerCase() === riskBand) &&
    (transactionType === 'all' || assessment.transaction_type === transactionType)
  )
}

function App() {
  const [startAt, setStartAt] = useState('2026-01-01T00:00')
  const [endAt, setEndAt] = useState('2026-01-02T00:00')
  const [validationError, setValidationError] = useState('')
  const [requestError, setRequestError] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [result, setResult] = useState<AssessmentResponse | null>(null)
  const [lastRequest, setLastRequest] = useState<AssessmentRequest | null>(null)
  const [selectedTransactionRef, setSelectedTransactionRef] = useState<string | null>(null)
  const [searchText, setSearchText] = useState('')
  const [riskBandFilter, setRiskBandFilter] = useState('all')
  const [transactionTypeFilter, setTransactionTypeFilter] = useState('all')
  const [sortOrder, setSortOrder] = useState<AssessmentSort>('score_desc')
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone
  const assessments = result?.assessments ?? []
  const bandCounts = getRiskBandCounts(assessments)
  const riskBandOptions = Array.from(
    new Map(
      [...riskBands, ...assessments.map((assessment) => assessment.risk_band)]
        .filter((band) => band.trim().length > 0)
        .map((band) => [band.toLocaleLowerCase(), band] as const),
    ).values(),
  )
  const transactionTypeOptions = Array.from(
    new Set(assessments.map((assessment) => assessment.transaction_type)),
  ).sort((left, right) => left.localeCompare(right))
  const normalizedSearch = searchText.trim().toLocaleLowerCase()
  const filteredAssessments = assessments
    .filter((assessment) =>
      matchesAssessmentFilters(
        assessment,
        normalizedSearch,
        riskBandFilter,
        transactionTypeFilter,
      ),
    )
    .sort((left, right) => {
      switch (sortOrder) {
        case 'timestamp_desc':
          return Date.parse(right.occurred_at) - Date.parse(left.occurred_at)
        case 'timestamp_asc':
          return Date.parse(left.occurred_at) - Date.parse(right.occurred_at)
        case 'amount_desc':
          return right.amount_minor_units - left.amount_minor_units
        case 'amount_asc':
          return left.amount_minor_units - right.amount_minor_units
        case 'score_desc':
          return right.score - left.score
      }
    })
  const selectedAssessment =
    filteredAssessments.find(
      (assessment) => assessment.transaction_ref === selectedTransactionRef,
    ) ??
    null

  function clearSelectionIfHidden(
    search: string,
    riskBand: string,
    transactionType: string,
  ) {
    if (!selectedTransactionRef) return
    const selected = assessments.find(
      (assessment) => assessment.transaction_ref === selectedTransactionRef,
    )
    if (
      !selected ||
      !matchesAssessmentFilters(selected, search, riskBand, transactionType)
    ) {
      setSelectedTransactionRef(null)
    }
  }

  function updateSearch(value: string) {
    setSearchText(value)
    clearSelectionIfHidden(value, riskBandFilter, transactionTypeFilter)
  }

  function updateRiskBandFilter(value: string) {
    setRiskBandFilter(value)
    clearSelectionIfHidden(searchText, value, transactionTypeFilter)
  }

  function updateTransactionTypeFilter(value: string) {
    setTransactionTypeFilter(value)
    clearSelectionIfHidden(searchText, riskBandFilter, value)
  }

  async function submitAssessment(request: AssessmentRequest) {
    setIsLoading(true)
    setRequestError('')
    setResult(null)
    setSelectedTransactionRef(null)
    setLastRequest(request)

    try {
      const response = await createAssessment(request)
      setResult(response)
      setSelectedTransactionRef(null)
    } catch (error) {
      setRequestError(
        error instanceof AssessmentApiError
          ? error.message
          : 'An unexpected error occurred. Please try again.',
      )
    } finally {
      setIsLoading(false)
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setValidationError('')
    setRequestError('')
    setResult(null)
    setSelectedTransactionRef(null)

    if (!startAt || !endAt) {
      setValidationError('Enter both a start and end date and time.')
      return
    }

    const start = new Date(startAt)
    const end = new Date(endAt)
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
      setValidationError('Enter valid start and end date and times.')
      return
    }
    if (end <= start) {
      setValidationError('The end time must be later than the start time.')
      return
    }

    void submitAssessment({
      start_at: start.toISOString(),
      end_at: end.toISOString(),
    })
  }

  function updatePeriod(setter: (value: string) => void, value: string) {
    setter(value)
    setValidationError('')
    setRequestError('')
    setResult(null)
    setSelectedTransactionRef(null)
  }

  return (
    <main className="min-h-screen bg-slate-100 px-4 py-10 text-slate-900 sm:px-6 lg:py-16">
      <div className="mx-auto max-w-4xl">
        <header className="mb-9">
          <div className="mb-5 flex items-center justify-between border-b border-slate-200 pb-4">
            <p className="text-sm font-bold uppercase tracking-[0.16em] text-indigo-800">
              YottaFlex.ai
            </p>
            <span className="rounded-full border border-slate-300 bg-white px-3 py-1 text-xs font-medium text-slate-600">
              Risk operations
            </span>
          </div>
          <h1 className="text-3xl font-semibold tracking-tight text-slate-950 sm:text-4xl">
            Transaction risk assessment
          </h1>
          <p className="mt-3 max-w-2xl text-base leading-7 text-slate-600">
            Select a time period to review transactions against deterministic
            risk indicators. A finding is an indicator for investigation, not a
            confirmation of fraud.
          </p>
        </header>

        <section
          aria-labelledby="period-heading"
          className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm sm:p-8"
        >
          <div className="mb-6">
            <h2 id="period-heading" className="text-xl font-semibold text-slate-950">
              Assessment period
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              Times are entered in your local timezone ({timezone || 'local time'})
              and sent to the service in UTC.
            </p>
          </div>

          <form onSubmit={handleSubmit} noValidate>
            <div className="grid gap-5 sm:grid-cols-2">
              <div>
                <label
                  htmlFor="start-at"
                  className="mb-2 block text-sm font-medium text-slate-700"
                >
                  Start date and time
                </label>
                <input
                  id="start-at"
                  name="start_at"
                  type="datetime-local"
                  required
                  value={startAt}
                  onChange={(event) => updatePeriod(setStartAt, event.target.value)}
                  aria-invalid={Boolean(validationError)}
                  aria-describedby={validationError ? 'period-error' : undefined}
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-900 shadow-sm outline-none transition focus:border-indigo-600 focus:ring-2 focus:ring-indigo-600/20"
                />
              </div>
              <div>
                <label
                  htmlFor="end-at"
                  className="mb-2 block text-sm font-medium text-slate-700"
                >
                  End date and time
                </label>
                <input
                  id="end-at"
                  name="end_at"
                  type="datetime-local"
                  required
                  value={endAt}
                  onChange={(event) => updatePeriod(setEndAt, event.target.value)}
                  aria-invalid={Boolean(validationError)}
                  aria-describedby={validationError ? 'period-error' : undefined}
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-900 shadow-sm outline-none transition focus:border-indigo-600 focus:ring-2 focus:ring-indigo-600/20"
                />
              </div>
            </div>

            {validationError && (
              <p id="period-error" role="alert" className="mt-3 text-sm text-red-700">
                {validationError}
              </p>
            )}

            <div className="mt-6 flex flex-wrap items-center gap-4">
              <button
                type="submit"
                disabled={isLoading}
                className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-indigo-700 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-700 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {isLoading && (
                  <span
                    aria-hidden="true"
                    className="size-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
                  />
                )}
                {isLoading ? 'Assessing…' : 'Run assessment'}
              </button>
              {isLoading && (
                <span role="status" className="text-sm text-slate-600">
                  Loading assessment results…
                </span>
              )}
            </div>
          </form>

          {requestError && (
            <div
              role="alert"
              className="mt-6 flex flex-col gap-3 rounded-xl border border-red-200 bg-red-50 p-4 sm:flex-row sm:items-center sm:justify-between"
            >
              <p className="text-sm leading-6 text-red-800">{requestError}</p>
              {lastRequest && (
                <button
                  type="button"
                  onClick={() => void submitAssessment(lastRequest)}
                  disabled={isLoading}
                  className="shrink-0 rounded-lg border border-red-300 bg-white px-4 py-2 text-sm font-semibold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-700 disabled:opacity-60"
                >
                  Retry
                </button>
              )}
            </div>
          )}

          {result && (
            <section
              aria-live="polite"
              aria-labelledby="result-heading"
              className={`mt-6 rounded-xl border p-5 ${
                result.assessment_count === 0
                  ? 'border-slate-200 bg-slate-50'
                  : 'border-emerald-200 bg-emerald-50'
              }`}
            >
              <h2 id="result-heading" className="font-semibold text-slate-950">
                {result.assessment_count === 0
                  ? 'No assessments for this period'
                  : 'Assessment complete'}
              </h2>
              <p className="mt-1 text-sm leading-6 text-slate-700">
                {result.assessment_count === 0
                  ? 'The service returned no target transactions in the selected period.'
                  : `${result.assessment_count} ${
                      result.assessment_count === 1 ? 'assessment' : 'assessments'
                    } returned for the selected period.`}
              </p>
            </section>
          )}
        </section>

        {result && assessments.length > 0 && (
          <div className="mt-8 space-y-6">
            <section aria-labelledby="overview-heading">
              <div className="mb-4 flex flex-wrap items-end justify-between gap-2">
                <div>
                  <h2 id="overview-heading" className="text-xl font-semibold text-slate-950">
                    Risk overview
                  </h2>
                  <p className="mt-1 text-sm text-slate-600">
                    {formatTimestamp(result.start_at)} – {formatTimestamp(result.end_at)}
                  </p>
                </div>
                <span className="text-sm text-slate-600">
                  {assessments.length} {assessments.length === 1 ? 'transaction assessed' : 'transactions assessed'}
                </span>
              </div>

              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                <div className="summary-card summary-total">
                  <span className="summary-label">Total assessed</span>
                  <span className="summary-value">{assessments.length}</span>
                </div>
                {riskBands.map((band) => (
                  <div key={band} className={`summary-card summary-${band}`}>
                    <span className="summary-label">{formatRiskBand(band)} risk</span>
                    <span className="summary-value">{bandCounts.counts[band]}</span>
                  </div>
                ))}
                {bandCounts.other > 0 && (
                  <div className="summary-card summary-other">
                    <span className="summary-label">Other risk band</span>
                    <span className="summary-value">{bandCounts.other}</span>
                  </div>
                )}
              </div>
              <p className="mt-3 text-xs text-slate-500">
                Risk-band totals are calculated from returned assessments; each transaction is counted once.
              </p>
            </section>

            <section
              aria-labelledby="transactions-heading"
              className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm"
            >
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-5 py-4 sm:px-6">
                <div>
                  <h2 id="transactions-heading" className="text-lg font-semibold text-slate-950">
                    Assessed transactions
                  </h2>
                  <p className="mt-1 text-sm text-slate-600">
                    Select a transaction reference to mark it for review.
                  </p>
                </div>
                <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600">
                  {result.assessment_count} {result.assessment_count === 1 ? 'total assessment' : 'total assessments'}
                </span>
              </div>

              <div className="grid gap-3 border-b border-slate-200 bg-slate-50/70 p-4 sm:grid-cols-2 lg:grid-cols-4 sm:px-6">
                <div className="lg:col-span-2">
                  <label htmlFor="assessment-search" className="filter-label">
                    Search transaction or account
                  </label>
                  <input
                    id="assessment-search"
                    type="search"
                    value={searchText}
                    onChange={(event) => updateSearch(event.target.value)}
                    placeholder="Reference or account reference"
                    className="filter-control"
                  />
                </div>
                <div>
                  <label htmlFor="risk-band-filter" className="filter-label">
                    Risk band
                  </label>
                  <select
                    id="risk-band-filter"
                    value={riskBandFilter}
                    onChange={(event) => updateRiskBandFilter(event.target.value)}
                    className="filter-control"
                  >
                    <option value="all">All risk bands</option>
                    {riskBandOptions.map((band) => (
                      <option key={band.toLocaleLowerCase()} value={band.toLocaleLowerCase()}>
                        {formatRiskBand(band)}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor="transaction-type-filter" className="filter-label">
                    Transaction type
                  </label>
                  <select
                    id="transaction-type-filter"
                    value={transactionTypeFilter}
                    onChange={(event) => updateTransactionTypeFilter(event.target.value)}
                    className="filter-control"
                  >
                    <option value="all">All transaction types</option>
                    {transactionTypeOptions.map((transactionType) => (
                      <option key={transactionType} value={transactionType}>
                        {transactionTypeLabels[transactionType] ?? transactionType}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="sm:col-span-2 lg:col-span-4">
                  <label htmlFor="assessment-sort" className="filter-label">
                    Sort assessments
                  </label>
                  <select
                    id="assessment-sort"
                    value={sortOrder}
                    onChange={(event) => setSortOrder(event.target.value as AssessmentSort)}
                    className="filter-control sm:max-w-sm"
                  >
                    <option value="score_desc">Risk score — highest first</option>
                    <option value="timestamp_desc">Occurrence time — newest first</option>
                    <option value="timestamp_asc">Occurrence time — oldest first</option>
                    <option value="amount_desc">Amount — highest first</option>
                    <option value="amount_asc">Amount — lowest first</option>
                  </select>
                </div>
              </div>

              <div
                className="flex flex-wrap items-center justify-between gap-2 px-5 py-3 text-sm sm:px-6"
                aria-live="polite"
              >
                <p className="text-slate-700">
                  <span className="font-semibold">{result.assessment_count}</span>{' '}
                  {result.assessment_count === 1 ? 'assessment returned' : 'assessments returned'}
                  <span className="mx-2 text-slate-300" aria-hidden="true">·</span>
                  <span className="font-semibold">{filteredAssessments.length}</span>{' '}
                  {filteredAssessments.length === 1 ? 'row visible' : 'rows visible'}
                </p>
                <p className="text-xs text-slate-500">
                  Sorted by {sortOrder === 'score_desc'
                    ? 'highest risk score first'
                    : sortOrder === 'timestamp_desc'
                      ? 'newest occurrence first'
                      : sortOrder === 'timestamp_asc'
                        ? 'oldest occurrence first'
                        : sortOrder === 'amount_desc'
                          ? 'highest amount first'
                          : 'lowest amount first'}
                </p>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full min-w-[850px] border-collapse text-left text-sm">
                  <caption className="sr-only">
                    Risk assessments for transactions in the selected assessment period
                  </caption>
                  <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th scope="col" className="px-5 py-3 font-semibold">Transaction reference</th>
                      <th scope="col" className="px-4 py-3 font-semibold">Occurred at</th>
                      <th scope="col" className="px-4 py-3 text-right font-semibold">Amount</th>
                      <th scope="col" className="px-4 py-3 font-semibold">Type</th>
                      <th scope="col" className="px-4 py-3 text-right font-semibold">Score</th>
                      <th scope="col" className="px-5 py-3 font-semibold">Risk band</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {filteredAssessments.map((assessment) => (
                      <AssessmentRow
                        key={assessment.transaction_ref}
                        assessment={assessment}
                        selected={assessment.transaction_ref === selectedTransactionRef}
                        onSelect={() => setSelectedTransactionRef(assessment.transaction_ref)}
                      />
                    ))}
                    {filteredAssessments.length === 0 && (
                      <tr>
                        <td colSpan={6} className="px-5 py-12 text-center">
                          <p className="font-semibold text-slate-800">
                            No transactions match these filters
                          </p>
                          <p className="mt-1 text-sm text-slate-600">
                            Adjust the search or filters to see assessments from this response.
                          </p>
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {selectedAssessment && (
                <div
                  aria-live="polite"
                  className="flex flex-wrap items-center justify-between gap-3 border-t border-indigo-100 bg-indigo-50 px-5 py-4 sm:px-6"
                >
                  <p className="text-sm text-indigo-950">
                    <span className="font-semibold">Selected transaction:</span>{' '}
                    <span className="font-mono">{selectedAssessment.transaction_ref}</span>
                    <span className="mx-2 text-indigo-300" aria-hidden="true">·</span>
                    Score <span className="font-semibold">{selectedAssessment.score}/100</span>
                  </p>
                  <button
                    type="button"
                    onClick={() => setSelectedTransactionRef(null)}
                    className="rounded-md px-2 py-1 text-sm font-medium text-indigo-800 underline decoration-indigo-300 underline-offset-2 hover:text-indigo-950 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-700"
                  >
                    Clear selection
                  </button>
                </div>
              )}
            </section>

            {selectedAssessment && result && (
              <InvestigationPanel
                assessment={selectedAssessment}
                startAt={result.start_at}
                endAt={result.end_at}
                onClose={() => setSelectedTransactionRef(null)}
              />
            )}
          </div>
        )}
      </div>
    </main>
  )
}

interface AssessmentRowProps {
  assessment: RiskAssessment
  selected: boolean
  onSelect: () => void
}

function AssessmentRow({ assessment, selected, onSelect }: AssessmentRowProps) {
  const bandClass = riskBands.includes(assessment.risk_band as KnownRiskBand)
    ? assessment.risk_band
    : 'other'

  return (
    <tr className={selected ? 'assessment-row assessment-row-selected' : 'assessment-row'}>
      <th scope="row" className="px-5 py-3.5 font-medium">
        <button
          type="button"
          aria-pressed={selected}
          onClick={onSelect}
          className="transaction-select rounded font-mono text-sm text-indigo-800 underline decoration-indigo-200 underline-offset-4 hover:text-indigo-950 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-indigo-700"
        >
          {assessment.transaction_ref}
          <span className="sr-only">{selected ? ', selected' : ', select transaction'}</span>
        </button>
      </th>
      <td className="whitespace-nowrap px-4 py-3.5 text-slate-600">
        {formatTimestamp(assessment.occurred_at)}
      </td>
      <td className="whitespace-nowrap px-4 py-3.5 text-right font-medium tabular-nums text-slate-900">
        {formatAmount(assessment.amount_minor_units, assessment.currency)}
      </td>
      <td className="whitespace-nowrap px-4 py-3.5 text-slate-600">
        {transactionTypeLabels[assessment.transaction_type] ?? assessment.transaction_type}
      </td>
      <td className="whitespace-nowrap px-4 py-3.5 text-right font-semibold tabular-nums text-slate-900">
        {assessment.score}<span className="font-normal text-slate-500">/100</span>
      </td>
      <td className="whitespace-nowrap px-5 py-3.5">
        <span className={`risk-badge risk-${bandClass}`}>
          <span className="risk-indicator" aria-hidden="true" />
          {formatRiskBand(assessment.risk_band)}
        </span>
      </td>
    </tr>
  )
}

interface InvestigationPanelProps {
  assessment: RiskAssessment
  startAt: string
  endAt: string
  onClose: () => void
}

function InvestigationPanel({
  assessment,
  startAt,
  endAt,
  onClose,
}: InvestigationPanelProps) {
  const riskClass = riskBands.includes(assessment.risk_band as KnownRiskBand)
    ? assessment.risk_band
    : 'other'

  return (
    <section
      aria-labelledby="investigation-heading"
      className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm"
    >
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-slate-200 px-5 py-5 sm:px-6">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-wide text-indigo-700">
            Transaction investigation
          </p>
          <h2
            id="investigation-heading"
            className="mt-1 break-all font-mono text-lg font-semibold text-slate-950"
          >
            {assessment.transaction_ref}
          </h2>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label={`Close investigation for ${assessment.transaction_ref}`}
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-700"
        >
          Close details
        </button>
      </div>

      <div className="space-y-7 p-5 sm:p-6">
        <section aria-labelledby="transaction-summary-heading">
          <h3 id="transaction-summary-heading" className="text-base font-semibold text-slate-950">
            Transaction summary
          </h3>
          <dl className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <DetailField label="Occurred at" value={formatTimestamp(assessment.occurred_at)} />
            <DetailField
              label="Amount"
              value={formatAmount(assessment.amount_minor_units, assessment.currency)}
            />
            <DetailField
              label="Transaction type"
              value={transactionTypeLabels[assessment.transaction_type] ?? assessment.transaction_type}
            />
            <DetailField label="Status" value={assessment.status} />
            <DetailField label="Source account" value={assessment.source_account_ref} />
            <DetailField
              label="Counterparty account"
              value={assessment.counterparty_account_ref ?? 'Not available'}
            />
            <div className="detail-field">
              <dt className="detail-label">Risk score</dt>
              <dd className="detail-value">{assessment.score}/100</dd>
            </div>
            <div className="detail-field">
              <dt className="detail-label">Risk band</dt>
              <dd>
                <span className={`risk-badge risk-${riskClass}`}>
                  <span className="risk-indicator" aria-hidden="true" />
                  {formatRiskBand(assessment.risk_band)}
                </span>
              </dd>
            </div>
          </dl>
        </section>

        <section aria-labelledby="score-contributions-heading">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div>
              <h3 id="score-contributions-heading" className="text-base font-semibold text-slate-950">
                Score contributions
              </h3>
              <p className="mt-1 text-sm text-slate-600">
                Final score and contribution values are reported by the risk engine.
              </p>
            </div>
            <p className="text-sm font-semibold text-slate-900">
              Final score: {assessment.score}/100 · {formatRiskBand(assessment.risk_band)}
            </p>
          </div>
          {assessment.contributions.length > 0 ? (
            <div className="mt-3 overflow-x-auto rounded-xl border border-slate-200">
              <table className="w-full min-w-[620px] text-left text-sm">
                <caption className="sr-only">
                  Backend scoring contributions for {assessment.transaction_ref}
                </caption>
                <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-4 py-3 font-semibold">Rule</th>
                    <th scope="col" className="px-4 py-3 font-semibold">Scoring family</th>
                    <th scope="col" className="px-4 py-3 text-right font-semibold">Configured</th>
                    <th scope="col" className="px-4 py-3 text-right font-semibold">Applied</th>
                    <th scope="col" className="px-4 py-3 font-semibold">Outcome</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {assessment.contributions.map((contribution, index) => (
                    <tr key={`${contribution.rule_id}-${index}`}>
                      <th scope="row" className="px-4 py-3 font-semibold text-slate-900">
                        {contribution.rule_id}
                      </th>
                      <td className="px-4 py-3 text-slate-600">
                        {contribution.scoring_family || 'Not provided'}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-slate-700">
                        {contribution.configured_points}
                      </td>
                      <td className="px-4 py-3 text-right font-semibold tabular-nums text-slate-900">
                        {contribution.applied_points}
                      </td>
                      <td className="px-4 py-3">
                        <ContributionOutcome
                          appliedPoints={contribution.applied_points}
                          deduplicatedBy={contribution.deduplicated_by}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="mt-3 rounded-lg bg-slate-50 px-4 py-3 text-sm text-slate-600">
              No score contributions were returned for this assessment.
            </p>
          )}
        </section>

        <section aria-labelledby="findings-heading">
          <div className="mb-3">
            <h3 id="findings-heading" className="text-base font-semibold text-slate-950">
              Findings and evidence
            </h3>
            <p className="mt-1 text-sm text-slate-600">
              Rule findings are risk indicators for review, not a determination of fraud.
            </p>
          </div>

          {assessment.findings.length === 0 ? (
            <p className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
              No rules triggered for this transaction in the assessment.
            </p>
          ) : (
            <div className="space-y-4">
              {assessment.findings.map((finding, index) => (
                <article
                  key={`${finding.rule_id}-${index}`}
                  className="min-w-0 rounded-xl border border-slate-200 bg-white"
                >
                  <div className="border-b border-slate-100 px-4 py-4 sm:px-5">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-xs font-semibold uppercase tracking-wide text-indigo-700">
                          {finding.rule_id}
                          {finding.severity && (
                            <span className="ml-2 rounded-full bg-slate-100 px-2 py-1 text-slate-700">
                              {formatRiskBand(finding.severity)} severity
                            </span>
                          )}
                        </p>
                        <h4 className="mt-2 break-words text-base font-semibold text-slate-950">
                          {finding.rule_name}
                        </h4>
                      </div>
                      <div className="flex gap-4 text-right text-xs sm:text-sm">
                        <div>
                          <p className="text-slate-500">Configured</p>
                          <p className="mt-1 font-semibold tabular-nums text-slate-800">
                            {finding.configured_points} pts
                          </p>
                        </div>
                        <div>
                          <p className="text-slate-500">Applied</p>
                          <p className="mt-1 font-semibold tabular-nums text-slate-900">
                            {finding.applied_points} pts
                          </p>
                        </div>
                      </div>
                    </div>
                    {finding.scoring_family && (
                      <p className="mt-3 text-xs text-slate-600">
                        Scoring family: <span className="font-medium">{finding.scoring_family}</span>
                      </p>
                    )}
                    {finding.deduplicated_by && (
                      <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm leading-6 text-amber-950">
                        This finding remains visible, but was deduplicated by{' '}
                        <span className="font-semibold">{finding.deduplicated_by}</span>. Its configured
                        points were not independently added to the score; the backend reports{' '}
                        {finding.applied_points} points applied.
                      </p>
                    )}
                    {!finding.deduplicated_by &&
                      finding.applied_points === 0 &&
                      finding.configured_points > 0 && (
                        <p className="mt-3 rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-700">
                          The backend reports that no points from this finding were applied.
                        </p>
                      )}
                    <p className="mt-3 break-words text-sm leading-6 text-slate-700">
                      {finding.explanation}
                    </p>
                  </div>

                  <div className="space-y-5 px-4 py-4 sm:px-5">
                    <EvidenceTransactions
                      transactions={finding.transactions}
                      transactionRefs={finding.transaction_refs}
                      selectedTransactionRef={assessment.transaction_ref}
                      startAt={startAt}
                      endAt={endAt}
                    />
                    {finding.account_refs.length > 0 && (
                      <div>
                        <h5 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                          Related accounts
                        </h5>
                        <ul className="mt-2 flex flex-wrap gap-2">
                          {finding.account_refs.map((accountRef, accountIndex) => (
                            <li
                              key={`${accountRef}-${accountIndex}`}
                              className="max-w-full break-all rounded-md bg-slate-100 px-2.5 py-1.5 font-mono text-xs text-slate-700"
                            >
                              {accountRef}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    <MetricList title="Observed metrics" metrics={finding.observed} />
                    <MetricList title="Rule thresholds" metrics={finding.threshold} />
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      </div>
    </section>
  )
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div className="detail-field min-w-0">
      <dt className="detail-label">{label}</dt>
      <dd className="detail-value break-words">{value}</dd>
    </div>
  )
}

function ContributionOutcome({
  appliedPoints,
  deduplicatedBy,
}: {
  appliedPoints: number
  deduplicatedBy: string | null
}) {
  if (deduplicatedBy) {
    return (
      <span className="inline-flex max-w-full flex-wrap gap-x-1 text-amber-800">
        <span className="font-medium">Deduplicated</span>
        <span>by {deduplicatedBy}</span>
      </span>
    )
  }
  if (appliedPoints > 0) {
    return <span className="font-medium text-emerald-800">Applied to score</span>
  }
  return <span className="text-slate-600">Not applied</span>
}

function EvidenceTransactions({
  transactions,
  transactionRefs,
  selectedTransactionRef,
  startAt,
  endAt,
}: {
  transactions: RiskAssessment['findings'][number]['transactions']
  transactionRefs: string[]
  selectedTransactionRef: string
  startAt: string
  endAt: string
}) {
  if (transactions.length === 0 && transactionRefs.length === 0) {
    return (
      <p className="text-sm text-slate-600">
        No transaction-level evidence was returned for this finding.
      </p>
    )
  }

  const evidenceRefs = new Set(transactions.map((transaction) => transaction.transaction_ref))
  const referenceOnly = transactionRefs.filter((reference) => !evidenceRefs.has(reference))

  return (
    <div>
      <h5 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
        Evidence transactions
      </h5>
      {transactions.length > 0 && (
        <ul className="mt-2 space-y-2">
          {transactions.map((transaction, index) => {
            const timestamp = new Date(transaction.occurred_at).getTime()
            const isOutsidePeriod =
              Number.isFinite(timestamp) &&
              (timestamp < new Date(startAt).getTime() ||
                timestamp >= new Date(endAt).getTime())
            const isSelectedTarget = transaction.transaction_ref === selectedTransactionRef
            const evidenceLabel = isSelectedTarget
              ? 'Selected target transaction'
              : isOutsidePeriod
                ? 'Supporting context outside period'
                : 'Other transaction in period'

            return (
              <li
                key={`${transaction.transaction_ref}-${index}`}
                className="min-w-0 rounded-lg border border-slate-200 bg-slate-50 p-3"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <span className="max-w-full break-all font-mono text-sm font-semibold text-slate-900">
                    {transaction.transaction_ref}
                  </span>
                  <span
                    className={
                      isSelectedTarget
                        ? 'evidence-label evidence-target'
                        : isOutsidePeriod
                          ? 'evidence-label evidence-context'
                          : 'evidence-label evidence-in-period'
                    }
                  >
                    {evidenceLabel}
                  </span>
                </div>
                <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-2">
                  <EvidenceField label="Occurred at" value={formatTimestamp(transaction.occurred_at)} />
                  <EvidenceField
                    label="Amount"
                    value={formatAmount(transaction.amount_minor_units, transaction.currency)}
                  />
                  <EvidenceField label="Status" value={transaction.status} />
                  <EvidenceField label="Currency" value={transaction.currency} />
                </dl>
              </li>
            )
          })}
        </ul>
      )}
      {referenceOnly.length > 0 && (
        <div className="mt-3">
          <p className="text-xs text-slate-600">
            Transaction references also returned without detailed evidence fields:
          </p>
          <ul className="mt-2 flex flex-wrap gap-2">
            {referenceOnly.map((reference, index) => (
              <li
                key={`${reference}-${index}`}
                className="max-w-full break-all rounded-md bg-slate-100 px-2.5 py-1.5 font-mono text-xs text-slate-700"
              >
                {reference}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function EvidenceField({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-0.5 break-words text-slate-800">{value}</dd>
    </div>
  )
}

function MetricList({
  title,
  metrics,
}: {
  title: string
  metrics: RiskAssessment['findings'][number]['observed']
}) {
  return (
    <div>
      <h5 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h5>
      {metrics.length > 0 ? (
        <dl className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {metrics.map((metric, index) => (
            <div
              key={`${metric.name}-${index}`}
              className="flex min-w-0 flex-wrap items-start justify-between gap-x-3 gap-y-1 rounded-lg bg-slate-50 px-3 py-2 text-sm"
            >
              <dt className="break-words text-slate-600">{metric.name}</dt>
              <dd className="max-w-full break-all font-medium text-slate-900">{metric.value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="mt-2 text-sm text-slate-500">Not provided.</p>
      )}
    </div>
  )
}

export default App