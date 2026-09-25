import { landmarks } from '../data/abuDhabi'

const landmarkExclusions = landmarks.map(landmark => ['>', ['distance', { type: 'Point', coordinates: landmark.coordinates }], landmark.id === 'mosque' ? 300 : 190])

export const mapStyle = {
  version: 8,
  glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
  sources: { city: { type: 'vector', url: 'https://tiles.openfreemap.org/planet', attribution: '<a href="https://openfreemap.org">OpenFreeMap</a> © <a href="https://www.openmaptiles.org/">OpenMapTiles</a> © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' } },
  light: { anchor: 'viewport', color: '#dbf2eb', intensity: 0.48, position: [1.5, 130, 48] },
  layers: [
    { id: 'ground', type: 'background', paint: { 'background-color': '#1a2b30' } },
    { id: 'landuse', type: 'fill', source: 'city', 'source-layer': 'landuse', paint: { 'fill-color': '#213337', 'fill-opacity': 0.5 } },
    { id: 'parks', type: 'fill', source: 'city', 'source-layer': 'landcover', filter: ['in', ['get', 'class'], ['literal', ['wood', 'grass']]], paint: { 'fill-color': '#234038', 'fill-opacity': 0.6 } },
    { id: 'water', type: 'fill', source: 'city', 'source-layer': 'water', paint: { 'fill-color': '#0a1a20' } },
    { id: 'coastline', type: 'line', source: 'city', 'source-layer': 'water', paint: { 'line-color': '#42696a', 'line-width': 1, 'line-opacity': 0.6 } },
    { id: 'roads', type: 'line', source: 'city', 'source-layer': 'transportation', filter: ['match', ['geometry-type'], ['LineString', 'MultiLineString'], true, false], paint: { 'line-color': '#526467', 'line-opacity': 0.5, 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 0.4, 15, 1.5, 19, 7] } },
    { id: 'major-roads', type: 'line', source: 'city', 'source-layer': 'transportation', filter: ['in', ['get', 'class'], ['literal', ['motorway', 'trunk', 'primary']]], paint: { 'line-color': '#789089', 'line-opacity': 0.6, 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 0.8, 15, 2.2, 19, 9] } },
    { id: 'buildings', type: 'fill-extrusion', source: 'city', 'source-layer': 'building', minzoom: 12, filter: ['all', ['!=', ['get', 'hide_3d'], true], ...landmarkExclusions], paint: { 'fill-extrusion-color': '#3b5056', 'fill-extrusion-height': ['coalesce', ['get', 'render_height'], 10], 'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0], 'fill-extrusion-opacity': 0.85, 'fill-extrusion-vertical-gradient': true } },
    { id: 'district-names', type: 'symbol', source: 'city', 'source-layer': 'place', minzoom: 10, filter: ['in', ['get', 'class'], ['literal', ['suburb', 'neighbourhood', 'quarter', 'city', 'town']]], layout: { 'text-field': ['coalesce', ['get', 'name:en'], ['get', 'name_en'], ['get', 'name']], 'text-font': ['Noto Sans Regular'], 'text-size': ['interpolate', ['linear'], ['zoom'], 10, 10, 15, 13], 'text-letter-spacing': 0.15, 'text-transform': 'uppercase', 'text-max-width': 10 }, paint: { 'text-color': '#afbdb7', 'text-halo-color': '#15282d', 'text-halo-width': 2 } },
    { id: 'road-names', type: 'symbol', source: 'city', 'source-layer': 'transportation_name', minzoom: 15, layout: { 'symbol-placement': 'line', 'text-field': ['coalesce', ['get', 'name:en'], ['get', 'name']], 'text-font': ['Noto Sans Regular'], 'text-size': 10 }, paint: { 'text-color': '#adbab9', 'text-halo-color': '#17292e', 'text-halo-width': 2 } },
  ],
}
