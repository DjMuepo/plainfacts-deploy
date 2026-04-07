import { useLocalSearchParams } from 'expo-router';
import { useEffect, useState } from 'react';
import { ScrollView, Text, View } from 'react-native';
import { getBriefs } from '../components/api';

export default function Search(){
  const { q } = useLocalSearchParams<{ q?: string }>();
  const [items, setItems] = useState<any[]>([]);
  useEffect(() => { if (q) getBriefs(q, 12).then(setItems).catch(()=>setItems([])); }, [q]);
  return <ScrollView contentContainerStyle={{ padding: 16, gap: 12 }}>
    <Text style={{ fontSize: 24, fontWeight: '800' }}>Search results</Text>
    <Text style={{ color: '#555' }}>{q}</Text>
    {items.map((item, i) => <View key={item.cluster_id || i} style={{ backgroundColor: '#fff', padding: 14, borderRadius: 16, borderWidth: 1, borderColor: '#ddd' }}><Text style={{ fontWeight: '800', marginBottom: 6 }}>{item.topic_title}</Text><Text style={{ color: '#555' }}>{item.what}</Text></View>)}
  </ScrollView>;
}
