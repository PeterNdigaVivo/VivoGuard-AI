// Plain-English labels for detector / alert types.
//
// Backend uses technical names everywhere (occupancy, queue,
// intrusion, …). Operators see store-friendly names everywhere
// (People in Store, Checkout Queue, After-hours Security, …).
//
// One central map so the same label is used by:
//   • Store dashboard "Detector activity" table
//   • Alerts feed (alert.detection_type → label)
//   • Anywhere else that touches the raw type strings
//
// Add new detectors here when they ship — leaving a fallback in
// `labelForDetector()` so unknown types degrade gracefully to a
// titlecased version of the raw string.

export const DETECTOR_LABELS: Record<string, string> = {
  // Continuous metrics
  occupancy:           'People in Store',
  occupancy_metrics:   'People in Store',
  unique_visitor:      'Customer Count',
  customer_journey:    'Customer Journey',
  heatmap:             'Busiest Areas',
  person:              'Person',
  vehicle:             'Vehicle',
  animal:              'Animal',
  lpr:                 'Plate Recognition',

  // Zone-based detectors
  queue:               'Checkout Queue',
  staff_present:       'Counter Staffing',
  staff_zone:          'Staff Area (Behind Counter)',
  dwell:               'Product Interest Time',
  entry_exit:          'Entry / Exit Counter',
  passersby:           'Window Passersby',
  window_engagement:   'Window Engagement',
  shutter:             'Store Open/Close',
  stockroom_access:    'Stockroom Access',
  shelf_change:        'Shelf Monitor',
  shelf:               'Shelf Empty',

  // Security / safety / incidents
  intrusion:           'After-hours Security',
  trespass:            'Restricted Area',
  loitering:           'Loitering Alert',
  tripwire:            'Line-cross Alert',
  tailgating:          'Tailgate Alert',
  shrinkage:           'Loss Prevention',
  fight:               'Incident Detection',
  fall:                'Fall Detection',
  abandoned_object:    'Abandoned Item',
  crowd:               'Crowd Alert',
  weapon:              'Weapon Detected',
  weapon_brandished:   'Weapon Drawn',
  fire:                'Fire Detected',
  smoke:               'Smoke Detected',

  // Training-required
  uniform_compliance:  'Uniform Compliance',
  demographic:         'Visitor Demographics',
  face:                'Face Detection',
}

export function labelForDetector(type: string): string {
  if (DETECTOR_LABELS[type]) return DETECTOR_LABELS[type]
  // Fallback: turn snake_case into Title Case so unknown types still
  // read like English on the dashboard.
  return type
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase())
}

// Categorise a detection type for the "Alerts by type" mini-chart
// grouping. Security incidents get their own bucket so operators
// notice them above operational signals.
export function categoryForDetector(type: string): 'security' | 'operations' | 'compliance' {
  if (['intrusion', 'trespass', 'loitering', 'tailgating', 'shrinkage',
       'fight', 'weapon', 'weapon_brandished', 'fire', 'smoke',
       'fall', 'abandoned_object'].includes(type)) return 'security'
  if (['uniform_compliance'].includes(type)) return 'compliance'
  return 'operations'
}
