export interface AssessmentRequest {
  start_at: string
  end_at: string
}

export interface TransactionEvidence {
  transaction_ref: string
  occurred_at: string
  amount_minor_units: number
  currency: string
  status: string
}

export interface EvidenceMetric {
  name: string
  value: number | string
}

export interface RuleFinding {
  rule_id: string
  rule_name: string
  severity: string
  transaction_refs: string[]
  transactions: TransactionEvidence[]
  account_refs: string[]
  observed: EvidenceMetric[]
  threshold: EvidenceMetric[]
  explanation: string
  configured_points: number
  applied_points: number
  scoring_family: string
  deduplicated_by: string | null
}

export interface RuleScoreContribution {
  rule_id: string
  configured_points: number
  applied_points: number
  scoring_family: string
  deduplicated_by: string | null
}

export interface RiskAssessment {
  transaction_ref: string
  occurred_at: string
  amount_minor_units: number
  currency: string
  transaction_type: string
  status: string
  source_account_ref: string
  counterparty_account_ref: string | null
  score: number
  risk_band: string
  findings: RuleFinding[]
  contributions: RuleScoreContribution[]
}

export interface AssessmentResponse {
  start_at: string
  end_at: string
  assessment_count: number
  assessments: RiskAssessment[]
}

export type AssessmentApiErrorKind =
  | 'validation'
  | 'limit'
  | 'unavailable'
  | 'http'
  | 'network'
  | 'invalid-response'

const API_PATH = '/api/risk/assessments'

export class AssessmentApiError extends Error {
  readonly kind: AssessmentApiErrorKind
  readonly status?: number

  constructor(
    kind: AssessmentApiErrorKind,
    message: string,
    status?: number,
  ) {
    super(message)
    this.name = 'AssessmentApiError'
    this.kind = kind
    this.status = status
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isDateTimeString(value: unknown): value is string {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value))
}

function isTransactionEvidence(value: unknown): value is TransactionEvidence {
  return (
    isRecord(value) &&
    typeof value.transaction_ref === 'string' &&
    isDateTimeString(value.occurred_at) &&
    Number.isInteger(value.amount_minor_units) &&
    typeof value.currency === 'string' &&
    typeof value.status === 'string'
  )
}

function isEvidenceMetric(value: unknown): value is EvidenceMetric {
  return (
    isRecord(value) &&
    typeof value.name === 'string' &&
    (typeof value.value === 'string' || Number.isInteger(value.value))
  )
}

function isRuleFinding(value: unknown): value is RuleFinding {
  return (
    isRecord(value) &&
    typeof value.rule_id === 'string' &&
    typeof value.rule_name === 'string' &&
    typeof value.severity === 'string' &&
    Array.isArray(value.transaction_refs) &&
    value.transaction_refs.every((item) => typeof item === 'string') &&
    Array.isArray(value.transactions) &&
    value.transactions.every(isTransactionEvidence) &&
    Array.isArray(value.account_refs) &&
    value.account_refs.every((item) => typeof item === 'string') &&
    Array.isArray(value.observed) &&
    value.observed.every(isEvidenceMetric) &&
    Array.isArray(value.threshold) &&
    value.threshold.every(isEvidenceMetric) &&
    typeof value.explanation === 'string' &&
    Number.isInteger(value.configured_points) &&
    Number.isInteger(value.applied_points) &&
    typeof value.scoring_family === 'string' &&
    (typeof value.deduplicated_by === 'string' ||
      value.deduplicated_by === null)
  )
}

function isScoreContribution(value: unknown): value is RuleScoreContribution {
  return (
    isRecord(value) &&
    typeof value.rule_id === 'string' &&
    Number.isInteger(value.configured_points) &&
    Number.isInteger(value.applied_points) &&
    typeof value.scoring_family === 'string' &&
    (typeof value.deduplicated_by === 'string' ||
      value.deduplicated_by === null)
  )
}

function isRiskAssessment(value: unknown): value is RiskAssessment {
  return (
    isRecord(value) &&
    typeof value.transaction_ref === 'string' &&
    isDateTimeString(value.occurred_at) &&
    Number.isInteger(value.amount_minor_units) &&
    typeof value.currency === 'string' &&
    typeof value.transaction_type === 'string' &&
    typeof value.status === 'string' &&
    typeof value.source_account_ref === 'string' &&
    (typeof value.counterparty_account_ref === 'string' ||
      value.counterparty_account_ref === null) &&
    Number.isInteger(value.score) &&
    typeof value.risk_band === 'string' &&
    Array.isArray(value.findings) &&
    value.findings.every(isRuleFinding) &&
    Array.isArray(value.contributions) &&
    value.contributions.every(isScoreContribution)
  )
}

function isAssessmentResponse(value: unknown): value is AssessmentResponse {
  if (!isRecord(value)) return false

  return (
    isDateTimeString(value.start_at) &&
    isDateTimeString(value.end_at) &&
    Number.isInteger(value.assessment_count) &&
    Array.isArray(value.assessments) &&
    value.assessments.every(isRiskAssessment)
  )
}

export async function createAssessment(
  request: AssessmentRequest,
): Promise<AssessmentResponse> {
  let response: Response
  try {
    response = await fetch(API_PATH, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    })
  } catch {
    throw new AssessmentApiError(
      'network',
      'The assessment service could not be reached. Check your connection and try again.',
    )
  }

  if (!response.ok) {
    if (response.status === 422) {
      throw new AssessmentApiError(
        'validation',
        'The server rejected the date range. Check the selected start and end times.',
        response.status,
      )
    }
    if (response.status === 413) {
      throw new AssessmentApiError(
        'limit',
        'This date range or its supporting data is too large. Choose a shorter period and try again.',
        response.status,
      )
    }
    if (response.status === 503) {
      throw new AssessmentApiError(
        'unavailable',
        'Assessment data is temporarily unavailable. Please try again shortly.',
        response.status,
      )
    }
    throw new AssessmentApiError(
      'http',
      'The assessment could not be completed because of a server error.',
      response.status,
    )
  }

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new AssessmentApiError(
      'invalid-response',
      'The server returned an unreadable assessment response.',
      response.status,
    )
  }

  if (!isAssessmentResponse(payload)) {
    throw new AssessmentApiError(
      'invalid-response',
      'The server returned an unexpected assessment response.',
      response.status,
    )
  }

  return payload
}
