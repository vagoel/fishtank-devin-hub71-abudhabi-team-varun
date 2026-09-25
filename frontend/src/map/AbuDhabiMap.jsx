import { useEffect, useRef, useState } from 'react'
import * as maplibregl from 'maplibre-gl'
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import { Compass, Expand, Layers3, Minus, Plus, MapPin, WifiOff } from 'lucide-react'
import 'maplibre-gl/dist/maplibre-gl.css'
import { colors, isActive, siteStatus } from '../domain/incidents'
import { footprint, landmarks } from '../data/abuDhabi'
import { mapStyle } from './mapStyle'
import { createLandmarkLayer } from './landmarkLayer'

const initialView = { center: [54.389, 24.474], zoom: 12.65, pitch: 55, bearing: -24 }
const emptyGeo = { type: 'FeatureCollection', features: [] }

function fitReportedLocations(map, sites) {
  if (!map || !sites.length) return
  const bounds = new maplibregl.LngLatBounds()
  sites.forEach(site => bounds.extend(site.coordinates))
  const compact = map.getContainer().clientWidth < 600
  map.fitBounds(bounds, { padding: { top: 80, bottom: 135, left: compact ? 45 : 190, right: compact ? 45 : 80 }, maxZoom: 14.8, duration: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 800 })
}

export default function AbuDhabiMap({ mode = 'demo', sites, incidents, selectedSiteId, stale, onSelectSite, view, dispatches }) {
  const host = useRef(null)
  const mapRef = useRef(null)
  const markers = useRef(new Map())
  const fittedLive = useRef(false)
  const select = useRef(onSelectSite)
  useEffect(() => { select.current = onSelectSite }, [onSelectSite])
  const [ready, setReady] = useState(false)
  const [issue, setIssue] = useState('')
  const [failed, setFailed] = useState(false)
  const [threeD, setThreeD] = useState(true)
  const [focusedLandmark, setFocusedLandmark] = useState(null)

  useEffect(() => {
    let map
    let disposed = false
    let timeout
    const landmarkMarkers = []
    try {
      maplibregl.setWorkerUrl(workerUrl)
      map = new maplibregl.Map({ container: host.current, style: mapStyle, ...initialView, maxBounds: mode === 'api' ? undefined : [[54.2, 24.28], [54.75, 24.7]], minZoom: mode === 'api' ? 3 : 10.5, maxZoom: 19, maxPitch: 70, attributionControl: { compact: true }, canvasContextAttributes: { antialias: true }, pixelRatio: Math.min(window.devicePixelRatio, 1.75) })
      mapRef.current = map
      map.on('load', () => {
        if (disposed) return
        map.addLayer(createLandmarkLayer())
        map.addSource('monitored-sites', { type: 'geojson', data: emptyGeo })
        map.addLayer({ id: 'site-outlines', type: 'line', source: 'monitored-sites', paint: { 'line-color': ['get', 'color'], 'line-width': 2, 'line-opacity': 0.9 } })
        map.addLayer({ id: 'site-buildings', type: 'fill-extrusion', source: 'monitored-sites', paint: { 'fill-extrusion-color': ['get', 'color'], 'fill-extrusion-height': ['get', 'height'], 'fill-extrusion-opacity': 0.65, 'fill-extrusion-base': 1 } })
        map.addSource('response-routes', { type: 'geojson', data: emptyGeo })
        map.addLayer({ id: 'response-lines', type: 'line', source: 'response-routes', paint: { 'line-color': '#82bdff', 'line-width': 2.5, 'line-dasharray': [2, 2], 'line-opacity': 0.85 } })
        map.addSource('response-units', { type: 'geojson', data: emptyGeo })
        map.addLayer({ id: 'response-unit-glow', type: 'circle', source: 'response-units', paint: { 'circle-color': '#82bdff', 'circle-radius': 12, 'circle-blur': 1, 'circle-opacity': 0.4 } })
        map.addLayer({ id: 'response-unit-marker', type: 'circle', source: 'response-units', paint: { 'circle-color': '#a8d6ff', 'circle-radius': 4, 'circle-stroke-color': '#fff', 'circle-stroke-width': 1.5 } })
        map.on('click', 'site-buildings', event => select.current(event.features[0].properties.id))
        map.on('mouseenter', 'site-buildings', () => { map.getCanvas().style.cursor = 'pointer' })
        map.on('mouseleave', 'site-buildings', () => { map.getCanvas().style.cursor = '' })
        for (const landmark of landmarks) {
          const element = document.createElement('button')
          element.className = 'landmark-marker'
          element.type = 'button'
          element.textContent = landmark.name
          element.setAttribute('aria-label', `View ${landmark.name} in 3D`)
          element.addEventListener('click', () => {
            setFocusedLandmark({ id: landmark.id, at: Date.now() })
            map.flyTo({ center: landmark.coordinates, zoom: landmark.zoom, offset: [0, landmark.id === 'etihad' ? 80 : 10], bearing: landmark.bearing, pitch: 62, duration: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 1000 })
          })
          landmarkMarkers.push(new maplibregl.Marker({ element, anchor: 'top', offset: [0, 12] }).setLngLat(landmark.coordinates).addTo(map))
        }
        setReady(true)
      })
      map.on('idle', () => { if (!disposed && map.isSourceLoaded('city')) { clearTimeout(timeout); setIssue('') } })
      map.on('error', event => { if (!disposed && event.error) setIssue('Some map data is unavailable. Incident monitoring is unaffected.') })
      map.getCanvas().addEventListener('webglcontextlost', () => { if (!disposed) { setIssue('3D rendering unavailable. Use the site list to continue.'); setFailed(true) } })
      timeout = setTimeout(() => { if (!disposed && !map.isSourceLoaded('city')) setIssue('Map tiles are taking longer to load. Check your connection.') }, 12000)
    } catch {
      queueMicrotask(() => {
        if (disposed) return
        setFailed(true)
        setIssue('This browser could not start the 3D map. All sites remain accessible in the list.')
      })
    }
    const markerCollection = markers.current
    return () => {
      disposed = true
      clearTimeout(timeout)
      markerCollection.forEach(marker => marker.remove())
      markerCollection.clear()
      landmarkMarkers.forEach(marker => marker.remove())
      map?.remove()
      mapRef.current = null
    }
  }, [mode])

  useEffect(() => {
    if (mode !== 'api' || !ready || fittedLive.current || !sites.length) return
    fittedLive.current = true
    if (!view) fitReportedLocations(mapRef.current, sites)
  }, [mode, ready, sites, view])

  useEffect(() => {
    if (!ready || !mapRef.current) return
    const map = mapRef.current
    map.getSource('monitored-sites')?.setData({ type: 'FeatureCollection', features: sites.filter(site => !site.incidentOnly).map(site => ({ type: 'Feature', id: site.id, properties: { id: site.id, color: colors[siteStatus(site, incidents, stale)], height: site.height ?? 35 }, geometry: { type: 'Polygon', coordinates: [site.footprint || footprint(site.coordinates)] } })) })
    const existing = new Set(sites.map(site => site.id))
    markers.current.forEach((marker, id) => { if (!existing.has(id)) { marker.remove(); markers.current.delete(id) } })
    for (const site of sites) {
      const status = siteStatus(site, incidents, stale)
      const count = incidents.filter(incident => incident.siteId === site.id && isActive(incident)).length
      let marker = markers.current.get(site.id)
      if (!marker) {
        const element = document.createElement('button')
        element.type = 'button'
        const label = document.createElement('span')
        label.className = 'site-beacon-label'
        const dot = document.createElement('span')
        dot.className = 'site-beacon-dot'
        element.append(label, dot)
        element.addEventListener('click', () => select.current(site.id))
        marker = new maplibregl.Marker({ element, anchor: 'bottom' }).setLngLat(site.coordinates).addTo(map)
        markers.current.set(site.id, marker)
      }
      marker.setLngLat(site.coordinates)
      const element = marker.getElement()
      element.className = `site-beacon ${status} ${selectedSiteId === site.id ? 'selected' : ''}`
      element.style.setProperty('--beacon-color', colors[status])
      element.querySelector('.site-beacon-label').textContent = `${site.code || site.id}${count ? ` · ${count} alert${count > 1 ? 's' : ''}` : ''}`
      element.setAttribute('aria-label', `${site.name}, ${status}, ${count} active alerts`)
    }
  }, [sites, incidents, selectedSiteId, stale, ready])

  useEffect(() => {
    if (!view || !ready) return
    mapRef.current?.flyTo({ center: view.coordinates, zoom: view.zoom || 15.2, pitch: mapRef.current.getPitch() > 5 ? 60 : 0, bearing: view.bearing ?? -24, duration: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 950, padding: { top: 40, bottom: 40, left: 0, right: 0 } })
  }, [view, ready])

  useEffect(() => {
    if (!ready) return
    const features = dispatches.filter(dispatch => dispatch.simulated && incidents.some(incident => incident.id === dispatch.incidentId && isActive(incident))).flatMap(dispatch => {
      const site = sites.find(item => item.id === dispatch.siteId)
      if (!site) return []
      return dispatch.services.map((service, index) => {
        const [x, y] = site.coordinates
        const origin = [x - 0.012 - index * 0.003, y - 0.008 + index * 0.005]
        return { type: 'Feature', properties: { service, createdAt: dispatch.createdAt, slot: index }, geometry: { type: 'LineString', coordinates: [origin, [x - 0.004, origin[1]], [x - 0.004, y], [x, y]] } }
      })
    })
    mapRef.current?.getSource('response-routes')?.setData({ type: 'FeatureCollection', features })
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const updateUnits = () => {
      if (document.visibilityState === 'hidden') return
      const units = features.map(route => {
        const progress = reduced ? 1 : Math.min(1, Math.max(0, (Date.now() - Date.parse(route.properties.createdAt) - 3000 - route.properties.slot * 800) / 22000))
        const segment = Math.min(2, Math.floor(progress * 3))
        const fraction = progress * 3 - segment
        const from = route.geometry.coordinates[segment]
        const to = route.geometry.coordinates[segment + 1]
        return { type: 'Feature', properties: route.properties, geometry: { type: 'Point', coordinates: [from[0] + (to[0] - from[0]) * fraction, from[1] + (to[1] - from[1]) * fraction] } }
      })
      mapRef.current?.getSource('response-units')?.setData({ type: 'FeatureCollection', features: units })
    }
    updateUnits()
    if (!features.length || reduced || features.every(route => Date.now() - Date.parse(route.properties.createdAt) > 25000 + route.properties.slot * 800)) return
    const timer = setInterval(updateUnits, 120)
    return () => clearInterval(timer)
  }, [dispatches, sites, incidents, ready])

  function overview() {
    setFocusedLandmark(null)
    if (mode === 'api' && sites.length) return fitReportedLocations(mapRef.current, sites)
    mapRef.current?.flyTo({ center: [54.449, 24.468], zoom: 11.75, pitch: threeD ? 48 : 0, bearing: -20, duration: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 1000 })
  }

  return (
    <div className="map-stage" onPointerDown={() => { fittedLive.current = true }} onKeyDown={() => { fittedLive.current = true }}>
      <div ref={host} className="map-canvas" aria-label="Interactive 3D map of Abu Dhabi construction sites" />
      {!ready && !failed && <div className="map-loading"><span className="loading-orbit" /><span>Bringing Abu Dhabi into focus</span></div>}
      {failed && <div className="map-fallback"><MapPin size={38} /><h3>City map unavailable</h3><p>Continue monitoring through the site and incident lists.</p></div>}
      {issue && <div className="map-notice" role="status"><WifiOff size={13} />{issue}</div>}
      <div className="map-coordinate"><span className={`status-dot ${stale ? 'offline' : 'healthy'}`} />{mode === 'api' ? 'REPORTED INCIDENT LOCATIONS' : <>ABU DHABI <span>24.4539° N · 54.3773° E</span></>}</div>
      <div className="map-controls">
        <button aria-label="Zoom in" onClick={() => mapRef.current?.zoomIn()}><Plus size={17} /></button>
        <button aria-label="Zoom out" onClick={() => mapRef.current?.zoomOut()}><Minus size={17} /></button>
        <span />
        <button aria-label="Reset north" onClick={() => mapRef.current?.resetNorth()}><Compass size={18} /></button>
        <button aria-label="Show all sites" onClick={overview}><Expand size={16} /></button>
        <button aria-label={threeD ? 'Switch to 2D map' : 'Switch to 3D map'} aria-pressed={threeD} onClick={() => { setThreeD(!threeD); mapRef.current?.easeTo({ pitch: threeD ? 0 : 55, duration: 400 }) }}><Layers3 size={17} /><small>{threeD ? '3D' : '2D'}</small></button>
      </div>
      <div className="landmark-tour">
        <span className="eyebrow">EXPLORE THE CITY</span>
        <div>{landmarks.map(landmark => <button className={focusedLandmark?.id === landmark.id && focusedLandmark.at >= (view?.at || 0) ? 'active' : ''} key={landmark.id} onClick={() => { setFocusedLandmark({ id: landmark.id, at: Date.now() }); setThreeD(true); mapRef.current?.flyTo({ center: landmark.coordinates, zoom: landmark.zoom, offset: [0, landmark.id === 'etihad' ? 80 : 10], pitch: 62, bearing: landmark.bearing, duration: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 1000 }) }}><span className={`landmark-glyph ${landmark.id}`} />{landmark.short}</button>)}</div>
      </div>
      <div className="map-legend"><span><i className="status-dot healthy" />No active alerts</span><span><i className="status-dot warning" />Warning</span><span><i className="status-dot critical" />Critical</span>{mode === 'api' && <span><i className="status-dot info" />Informational</span>}<span><i className="status-dot offline" />{mode === 'api' ? 'Stale source data' : 'Offline / stale'}</span></div>
      <span className="map-disclaimer">{mode === 'api' ? 'Incident locations · fallback assignments labeled demo · illustrative landmarks' : 'Illustrative 3D architecture · sample construction sites'}{dispatches.length > 0 ? ' · response routes simulated' : ''}</span>
    </div>
  )
}
