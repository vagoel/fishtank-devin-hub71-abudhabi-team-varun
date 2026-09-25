import { Component } from 'react'
import { MapPin } from 'lucide-react'
import { statusLabels } from '../domain/incidents'

export function StatusBadge({ status, children }) {
  return <span className={`status-badge ${status}`}><i className={`status-dot ${status}`} />{children || statusLabels[status] || status}</span>
}

export function Sparkline({ values = [], tone = 'healthy', label = 'Sample band temperature readings' }) {
  const min = Math.min(...values) - 0.3
  const max = Math.max(...values) + 0.3
  const points = values.map((value, index) => `${index / Math.max(1, values.length - 1) * 240},${45 - (value - min) / (max - min || 1) * 35}`).join(' ')
  return <svg className={`sparkline ${tone}`} viewBox="0 0 240 55" role="img" aria-label={label}><path d="M0 46H240 M0 24H240" className="chart-grid" /><polygon points={`0,55 ${points} 240,55`} className="chart-fill" /><polyline points={points} fill="none" className="chart-line" strokeWidth="2" /></svg>
}

export function EmptyState({ icon: Icon, title, children }) {
  return <div className="empty-state">{Icon && <Icon size={28} />}<h3>{title}</h3><p>{children}</p></div>
}

export class MapBoundary extends Component {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    return this.state.failed ? <div className="map-fallback"><MapPin size={36} /><h3>Map view unavailable</h3><p>The city visualization could not load. Continue reviewing workers and incidents through the lists.</p></div> : this.props.children
  }
}
