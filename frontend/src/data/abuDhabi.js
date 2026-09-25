export const landmarks = [
  { id: 'etihad', name: 'Etihad Towers', short: 'Etihad', coordinates: [54.321297, 24.459322], zoom: 15.35, bearing: -26, height: 305, reference: 'https://en.wikipedia.org/wiki/Etihad_Towers' },
  { id: 'louvre', name: 'Louvre Abu Dhabi', short: 'Louvre', coordinates: [54.398, 24.5337], zoom: 16.1, bearing: 22, height: 40, reference: 'https://en.wikipedia.org/wiki/Louvre_Abu_Dhabi' },
  { id: 'mosque', name: 'Sheikh Zayed Grand Mosque', short: 'Grand Mosque', coordinates: [54.474, 24.412], zoom: 15.8, bearing: -20, height: 104, reference: 'https://en.wikipedia.org/wiki/Sheikh_Zayed_Grand_Mosque' },
  { id: 'aldar', name: 'Aldar Headquarters', short: 'Aldar HQ', coordinates: [54.57528, 24.44111], zoom: 16.6, bearing: -35, height: 110, reference: 'https://en.wikipedia.org/wiki/Aldar_Headquarters_building' },
]

export const siteLocations = [
  { id: 'AD-01', name: 'Corniche Residences', district: 'Al Bateen', coordinates: [54.3328, 24.46], code: 'CR', height: 135 },
  { id: 'AD-02', name: 'Saadiyat Cultural Quarter', district: 'Saadiyat Island', coordinates: [54.4087, 24.5301], code: 'SQ', height: 48 },
  { id: 'AD-03', name: 'Reem Waterfront', district: 'Al Reem Island', coordinates: [54.4106, 24.4968], code: 'RW', height: 170 },
  { id: 'AD-04', name: 'Capital District Works', district: 'Al Rawdah', coordinates: [54.4597, 24.4185], code: 'CD', height: 65 },
  { id: 'AD-05', name: 'Al Raha Promenade', district: 'Al Raha Beach', coordinates: [54.5998, 24.4456], code: 'RP', height: 80 },
  { id: 'AD-06', name: 'Yas Marina Expansion', district: 'Yas Island', coordinates: [54.6023, 24.476], code: 'YM', height: 42 },
]

export function footprint([lng, lat], width = 0.00065) {
  const depth = width * 0.8
  return [[lng - width, lat - depth], [lng + width, lat - depth], [lng + width, lat + depth], [lng - width, lat + depth], [lng - width, lat - depth]]
}
