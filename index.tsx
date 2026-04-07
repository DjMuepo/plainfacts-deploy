import { useEffect, useState } from 'react';
import { Link } from 'expo-router';
import { ScrollView, Text, TextInput, TouchableOpacity, View } from 'react-native';
import { getBriefs } from '../components/api';

export default function Home(){
  const [query, setQuery] = useState('inflation');
  const [items, setItems] = useState<any[]>([]);
  useEffect(() => { getBriefs('world news', 6).then(setItems).catch(()=>setItems([])); }, []);
  return <ScrollView contentContainerStyle={{ padding: 16, gap: 12 }}>
    <Text style={{ fontSize: 28, fontWeight: '800' }}>PlainFacts mobile</Text>
    <Text style={{ color: '#555' }}>Expo starter for the same PlainFacts API.</Text>
    <View style={{ flexDirection: 'row', gap: 8 }}>
      <TextInput value={query} onChangeText={setQuery} placeholder="Search" style={{ flex: 1, backgroundColor: '#fff', borderRadius: 14, padding: 12, borderWidth: 1, borderColor: '#ddd' }} />
      <Link href={{ pathname: '/search', params: { q: query } }} asChild><TouchableOpacity style={{ backgroundColor: '#111', paddingHorizontal: 16, borderRadius: 14, justifyContent: 'center' }}><Text style={{ color: '#fff', fontWeight: '700' }}>Search</Text></TouchableOpacity></Link>
    </View>
    {items.map((item, i) => <View key={item.cluster_id || i} style={{ backgroundColor: '#fff', padding: 14, borderRadius: 16, borderWidth: 1, borderColor: '#ddd' }}><Text style={{ fontWeight: '800', marginBottom: 6 }}>{item.topic_title}</Text><Text style={{ color: '#555' }}>{item.what}</Text></View>)}
  </ScrollView>;
}
