import * as THREE from 'three'
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js'

function architecture() {
  const buckets = {}
  const palette = {
    stone: new THREE.MeshStandardMaterial({ color: '#d8d5bc', roughness: 0.65 }),
    glass: new THREE.MeshStandardMaterial({ color: '#75a6b0', metalness: 0.58, roughness: 0.25, emissive: '#17313c', emissiveIntensity: 0.3 }),
    trim: new THREE.MeshStandardMaterial({ color: '#adcecb', metalness: 0.6, roughness: 0.35 }),
    gold: new THREE.MeshStandardMaterial({ color: '#d6b67b', metalness: 0.5, roughness: 0.4 }),
    base: new THREE.MeshStandardMaterial({ color: '#55696a', roughness: 0.85 }),
  }
  function add(geometry, material, x = 0, y = 0, z = 0, rotation = [0, 0, 0]) {
    geometry.rotateX(rotation[0]); geometry.rotateY(rotation[1]); geometry.rotateZ(rotation[2]); geometry.translate(x, y, z)
    const flat = geometry.index ? geometry.toNonIndexed() : geometry.clone()
    flat.deleteAttribute('uv')
    geometry.dispose()
    ;(buckets[material] ||= []).push(flat)
  }
  function box(x, y, z, w, h, d, material = 'stone', rotation = 0) { add(new THREE.BoxGeometry(w, h, d), material, x, y, z, [0, rotation, 0]) }
  function cylinder(x, y, z, top, bottom, height, material = 'stone', segments = 24) { add(new THREE.CylinderGeometry(top, bottom, height, segments), material, x, y, z) }
  function dome(x, y, z, radius, height, material = 'stone') {
    const geometry = new THREE.SphereGeometry(radius, 32, 16, 0, Math.PI * 2, 0, Math.PI / 2)
    geometry.scale(1, height / radius, 1)
    add(geometry, material, x, y, z)
  }
  function finish() {
    const group = new THREE.Group()
    for (const [key, geometries] of Object.entries(buckets)) {
      const geometry = mergeGeometries(geometries)
      group.add(new THREE.Mesh(geometry, palette[key]))
      if (key === 'glass') group.add(new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 30), new THREE.LineBasicMaterial({ color: '#98c3c5', transparent: true, opacity: 0.45 })))
      geometries.forEach(item => item.dispose())
    }
    for (const [key, material] of Object.entries(palette)) if (!buckets[key]) material.dispose()
    return group
  }
  return { add, box, cylinder, dome, finish }
}

function etihad() {
  const a = architecture()
  a.box(0, 6, 0, 240, 12, 175, 'base')
  const towers = [[-80, -35, 278], [-23, -48, 305], [44, -12, 260], [80, 45, 234], [-10, 52, 218]]
  for (const [x, z, height] of towers) {
    for (let level = 0; level < 18; level++) {
      const t = level / 18
      const y = 12 + height * t
      const width = 18 * (1 - 0.28 * t)
      const drift = 10 * Math.sin(t * 1.9)
      const geometry = new THREE.CylinderGeometry(width * 0.98, width, height / 18 + 0.1, 14)
      geometry.scale(1, 1, 0.74)
      a.add(geometry, 'glass', x + drift, y + height / 36, z)
      const band = new THREE.CylinderGeometry(width + 0.2, width + 0.2, 0.6, 14)
      band.scale(1, 1, 0.74)
      a.add(band, 'trim', x + drift, y, z)
    }
    a.cylinder(x + 10, height + 19, z, 1.3, 1.3, 19, 'trim', 8)
  }
  return a.finish()
}

function louvre() {
  const a = architecture()
  a.box(0, 2, 0, 230, 4, 220, 'base')
  for (let x = -70; x <= 70; x += 35) for (let z = -70; z <= 70; z += 35) a.box(x, 8, z, 30, 12 + ((x + z + 140) % 3) * 2, 28)
  a.dome(0, 19, 0, 90, 21, 'trim')
  for (let ring = 1; ring <= 8; ring++) {
    const radius = ring * 10.5
    const y = 19 + 21 * Math.sqrt(1 - (radius / 90) ** 2)
    a.add(new THREE.TorusGeometry(radius, 0.5, 4, 80), 'gold', 0, y + 0.6, 0, [Math.PI / 2, 0, 0])
  }
  for (let i = 0; i < 24; i++) {
    const angle = i / 24 * Math.PI * 2
    const points = Array.from({ length: 17 }, (_, j) => {
      const r = j / 16 * 90
      const theta = angle + j * 0.045
      return new THREE.Vector3(Math.cos(theta) * r, 20 + 21 * Math.sqrt(1 - (r / 90) ** 2), Math.sin(theta) * r)
    })
    a.add(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(points), 18, 0.5, 4, false), 'gold')
  }
  return a.finish()
}

function mosque() {
  const a = architecture()
  a.box(0, 2, 0, 290, 4, 420, 'base')
  a.box(0, 5, 0, 250, 6, 370)
  a.box(0, 7, 20, 174, 2, 190, 'gold')
  a.box(0, 8, 20, 166, 2, 180)
  a.box(0, 21, -132, 235, 28, 75)
  for (const x of [-108, 108]) {
    a.box(x, 18, 30, 22, 20, 260)
    for (let z = -90; z <= 135; z += 25) {
      a.dome(x, 30, z, 12, 12)
      for (const dx of [-8, 8]) a.cylinder(x + dx, 17, z, 1.5, 1.5, 22, 'stone', 8)
    }
  }
  for (const x of [-75, 0, 75]) {
    const r = x === 0 ? 23 : 17
    a.cylinder(x, 34, -130, r, r, 12)
    a.dome(x, 40, -130, r, x === 0 ? 34 : 24)
    a.cylinder(x, x === 0 ? 77 : 67, -130, 0.4, 0.8, 9, 'gold', 8)
  }
  for (const x of [-123, 123]) for (const z of [-172, 172]) {
    a.cylinder(x, 26, z, 7, 10, 48)
    a.cylinder(x, 60, z, 4, 6, 28)
    a.cylinder(x, 78, z, 3, 5, 16)
    a.cylinder(x, 93, z, 0.5, 3, 22, 'gold')
    for (const y of [47, 73, 85]) a.cylinder(x, y, z, 8, 8, 3)
  }
  return a.finish()
}

function aldar() {
  const a = architecture()
  a.box(0, 3, 0, 138, 6, 58, 'base')
  a.add(new THREE.CylinderGeometry(57, 57, 27, 64), 'glass', 0, 56, 0, [Math.PI / 2, 0, 0])
  for (const z of [-14, 14]) {
    a.add(new THREE.TorusGeometry(56, 1.6, 8, 80), 'trim', 0, 56, z)
    for (const angle of [-Math.PI / 4, Math.PI / 4]) for (let i = -5; i <= 5; i++) {
      const offset = i * 9
      const half = Math.sqrt(54 ** 2 - offset ** 2)
      a.add(new THREE.BoxGeometry(half * 2, 0.7, 0.6), 'trim', -Math.sin(angle) * offset, 56 + Math.cos(angle) * offset, z, [0, 0, angle])
    }
  }
  const group = a.finish()
  group.rotation.y = Math.PI / 4
  return group
}

export function createLandmark(id) {
  return ({ etihad, louvre, mosque, aldar })[id]()
}
