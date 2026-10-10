const WHAT = {
  accounts: 'Equity, free and reserved cash, open positions, drawdown from peak and equity curve for P30, P60 and P100.',
  positions: 'Open positions, active GTTs, trade state and stop history, with paper edits on P60.',
  journal: 'Trade list with filters and per-trade LLM explanations; CSV export.',
  learning: 'Weekly reviews, proposals, champion vs challenger results and config versions.',
}

export default function Placeholder({ screen }) {
  return (
    <>
      <h1>{screen.label}</h1>
      <div className="card empty">
        <p className="muted">Arrives in Phase {screen.phase}.</p>
        <p>{WHAT[screen.id]}</p>
      </div>
    </>
  )
}
