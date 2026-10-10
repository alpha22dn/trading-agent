// Small stroke icons for the drawing toolbar (24×24, currentColor).
const P = {
  cursor: <path d="M6 4l12 7-5.5 1.5L10 18z" />,
  segment: <><path d="M5 19L19 5" /><circle cx="5" cy="19" r="1.8" /><circle cx="19" cy="5" r="1.8" /></>,
  rayLine: <><path d="M5 19L21 3" /><circle cx="5" cy="19" r="1.8" /><circle cx="12" cy="12" r="1.8" /></>,
  straightLine: <><path d="M2 22L22 2" /><circle cx="8" cy="16" r="1.8" /><circle cx="16" cy="8" r="1.8" /></>,
  arrowLine: <><path d="M5 19L18 6" /><path d="M11 6h7v7" /></>,
  horizontalStraightLine: <><path d="M2 12h20" /><circle cx="12" cy="12" r="1.8" /></>,
  horizontalRayLine: <><path d="M6 12h16" /><circle cx="6" cy="12" r="1.8" /></>,
  verticalStraightLine: <><path d="M12 2v20" /><circle cx="12" cy="12" r="1.8" /></>,
  priceLine: <><path d="M4 12h12" /><path d="M16 9h5v6h-5z" /></>,
  parallelStraightLine: <><path d="M3 15L15 3M9 21L21 9" /></>,
  priceChannelLine: <><path d="M3 9L21 3M3 15L21 9M3 21L21 15" /></>,
  fibonacciLine: <><path d="M3 5h18M3 10h18M3 14h18M3 19h18" /></>,
  fibExtension: <><path d="M3 19l6-10 4 5" /><path d="M13 14h8M13 9h8M13 4h8" /></>,
  rectangle: <rect x="4" y="6" width="16" height="12" rx="1" />,
  circleShape: <circle cx="12" cy="12" r="8" />,
  triangle: <path d="M12 4l9 16H3z" />,
  brush: <path d="M4 18c3 0 3-4 6-4s2 4 5 2 3-8 5-10" />,
  textLabel: <><path d="M5 6h14M12 6v13" /></>,
  longPosition: <><rect x="4" y="4" width="16" height="8" /><rect x="4" y="12" width="16" height="6" /><path d="M8 9l2-2 2 2" /></>,
  shortPosition: <><rect x="4" y="6" width="16" height="6" /><rect x="4" y="12" width="16" height="8" /><path d="M8 15l2 2 2-2" /></>,
  priceRange: <><path d="M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4" /></>,
  magnet: <path d="M6 4v8a6 6 0 0012 0V4h-4v8a2 2 0 01-4 0V4zM6 8h4M14 8h4" />,
  lock: <><rect x="5" y="11" width="14" height="9" rx="1.5" /><path d="M8 11V8a4 4 0 018 0v3" /></>,
  eye: <><path d="M2 12s4-6 10-6 10 6 10 6-4 6-10 6S2 12 2 12z" /><circle cx="12" cy="12" r="2.5" /></>,
  trash: <><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" /></>,
}

export function ToolIcon({ name }) {
  return <svg viewBox="0 0 24 24" className="rs-ico" aria-hidden="true">{P[name] || P.segment}</svg>
}
