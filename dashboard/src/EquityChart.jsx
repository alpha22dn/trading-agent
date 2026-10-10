import { useEffect, useRef, useState } from 'react'
import { ColorType, CrosshairMode, LineSeries, LineStyle, createChart } from 'lightweight-charts'

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()

// One line per account, as % return on starting capital so the three sizes compare directly.
// Each line is labelled on the price axis with its account id; the legend repeats the colours.
// Optional per series: `color` (a CSS variable name), `dashed`, `label` (backtest comparisons).
export default function EquityChart({ accounts, height = 300, emptyText }) {
  const box = useRef(null)
  const [scheme, setScheme] = useState(0)
  const points = accounts.reduce((n, a) => n + a.equity_curve.length, 0)

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const on = () => setScheme((s) => s + 1)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])

  useEffect(() => {
    if (!points || !box.current) return
    const chart = createChart(box.current, {
      height,
      layout: { background: { type: ColorType.Solid, color: css('--surface') }, textColor: css('--text-2'), fontFamily: 'inherit', attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: css('--border') } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
      crosshair: { mode: CrosshairMode.Magnet },
      localization: { locale: 'en-IN', priceFormatter: (v) => `${v > 0 ? '+' : ''}${v.toFixed(2)}%` },
    })
    let first = true
    for (const a of accounts) {
      const s = chart.addSeries(LineSeries, {
        color: css(a.color || `--acct-${a.id}`) || css('--series-1'), lineWidth: 2, title: a.label || a.id,
        lineStyle: a.dashed ? LineStyle.Dashed : LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: true, pointMarkersVisible: a.equity_curve.length < 3,
      })
      s.setData(a.equity_curve.map((p) => ({ time: p.date, value: (p.equity / a.capital - 1) * 100 })))
      if (first) {
        s.createPriceLine({ price: 0, color: css('--muted'), lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: '' })
        first = false
      }
    }
    chart.timeScale().fitContent()
    const ro = new ResizeObserver(() => chart.applyOptions({ width: box.current.clientWidth }))
    ro.observe(box.current)
    return () => { ro.disconnect(); chart.remove() }
  }, [accounts, height, points, scheme])

  if (!points) return <p className="muted">{emptyText || 'The curve starts with the first EOD run after Phase 2 is installed.'}</p>
  return (
    <div>
      <div ref={box} style={{ width: '100%' }} role="img" aria-label="Equity curves of the paper accounts as percent return" />
      <div className="chart-legend fine">
        {accounts.map((a) => (
          <span key={a.label || a.id}>
            {a.color || a.dashed
              ? <i className={`lg ${a.dashed ? 'lg-dashed' : 'lg-solid'}`} style={{ borderColor: `var(${a.color || `--acct-${a.id}`})` }} />
              : <i className={`swatch sw-${a.id}`} />} {a.label || a.id}
          </span>
        ))}
        <span><i className="lg lg-bb" /> Starting capital (0%)</span>
      </div>
    </div>
  )
}
