import * as THREE from 'three'
import { MercatorCoordinate } from 'maplibre-gl'
import { landmarks } from '../data/abuDhabi'
import { createLandmark } from './landmarks'

export function createLandmarkLayer() {
  let renderer
  let scene
  let camera
  const origin = MercatorCoordinate.fromLngLat([54.4, 24.48], 0)
  const scale = origin.meterInMercatorCoordinateUnits()
  const world = new THREE.Matrix4().makeTranslation(origin.x, origin.y, origin.z)
    .scale(new THREE.Vector3(scale, -scale, scale))
    .multiply(new THREE.Matrix4().makeRotationX(Math.PI / 2))
  return {
    id: 'abu-dhabi-landmarks', type: 'custom', renderingMode: '3d',
    onAdd(map, gl) {
      camera = new THREE.Camera()
      scene = new THREE.Scene()
      scene.add(new THREE.AmbientLight('#c4e2e5', 2.2))
      const sun = new THREE.DirectionalLight('#fff0cf', 3.2)
      sun.position.set(-200, 500, 300)
      scene.add(sun)
      for (const landmark of landmarks) {
        const position = MercatorCoordinate.fromLngLat(landmark.coordinates)
        const model = createLandmark(landmark.id)
        model.position.set((position.x - origin.x) / scale, 1, (position.y - origin.y) / scale)
        scene.add(model)
      }
      renderer = new THREE.WebGLRenderer({ canvas: map.getCanvas(), context: gl, antialias: true })
      renderer.autoClear = false
      renderer.toneMapping = THREE.ACESFilmicToneMapping
      renderer.toneMappingExposure = 1.2
    },
    render(gl, args) {
      camera.projectionMatrix.fromArray(args.defaultProjectionData.mainMatrix).multiply(world)
      renderer.resetState()
      renderer.render(scene, camera)
      renderer.resetState()
    },
    onRemove() {
      const materials = new Set()
      scene?.traverse(object => {
        object.geometry?.dispose()
        if (object.material) (Array.isArray(object.material) ? object.material : [object.material]).forEach(material => materials.add(material))
      })
      materials.forEach(material => material.dispose())
      renderer?.dispose()
    },
  }
}
