import { Component } from 'react'
import { Icon } from './common'

/** Contains a crash to the part of the UI it wraps instead of white-
 *  screening the whole dashboard. React requires a class component for
 *  this — there is no hook equivalent. */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }
  static getDerivedStateFromError(error) { return { error } }
  componentDidCatch(error, info) {
    console.error(`[AtlasOS] ${this.props.label || 'a panel'} crashed:`, error, info.componentStack)
  }
  render() {
    if (!this.state.error) return this.props.children
    const label = this.props.label || 'this page'
    return (
      <div className="nodata" role="alert" style={{ display: 'flex', flexDirection: 'column', gap: 10, margin: this.props.inline ? 0 : undefined }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 15, fontWeight: 500, color: 'var(--st-crit)' }}>
          <Icon name="ph-warning-octagon" size={19} />Something broke on {label}
        </div>
        <div className="muted" style={{ fontSize: 12.5 }}>The rest of the dashboard is unaffected — this is contained to {label} only.</div>
        <details style={{ fontSize: 11.5 }}>
          <summary className="muted" style={{ cursor: 'pointer' }}>Technical details</summary>
          <code className="term" style={{ display: 'block', marginTop: 6, padding: '8px 10px', fontSize: 11, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {String(this.state.error?.stack || this.state.error)}
          </code>
        </details>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn btn-secondary" onClick={() => this.setState({ error: null })}><Icon name="ph-arrow-clockwise" />Try again</button>
          {this.props.onReset && (
            <button className="btn btn-ghost" onClick={() => { this.setState({ error: null }); this.props.onReset() }}>Go to Overview</button>
          )}
        </div>
      </div>
    )
  }
}
