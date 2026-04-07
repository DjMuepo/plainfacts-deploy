export const API_BASE = process.env.EXPO_PUBLIC_PLAINFOFACTS_API || 'http://localhost:8000';
export async function getBriefs(q: string, maxClusters = 10){
  const res = await fetch(`${API_BASE}/briefs?q=${encodeURIComponent(q)}&max_clusters=${maxClusters}`);
  if(!res.ok) throw new Error('API error');
  return res.json();
}
