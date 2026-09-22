# FinTR-LLM ürün spesifikasyonu

Durum: Taslak v0.1  
Kapsam: Ürün davranışı ve değerlendirme gereksinimleri

## 1. Amaç

FinTR-LLM; KAP açıklamaları, finansal raporlar, faaliyet raporları ve finansal
dipnotları Türkçe olarak açıklayan, önemli bilgileri çıkaran, dönemsel değişimleri
karşılaştıran ve analiz için veri yeterliliğini değerlendiren bir doküman analiz
sistemidir. Planlanan yöntem, açık ağırlıklı bir base LLM üzerinde QLoRA/SFT ve
güncel bilgiyi veri-tarihli retrieval ile sağlamaktır.

## 2. Kapsam dışı davranışlar

Sistem:

- yatırım tavsiyesi vermez, hedef fiyat veya doğrudan al/sat kararı üretmez;
- kaynakta bulunmayan rakam, tarih, olay veya açıklama üretmez;
- fiyat hareketini finansal raporun kendisiyle karıştırmaz;
- endeks üyeliğini finansal rapor doğruluğunun veya şirket kalitesinin kanıtı
  saymaz;
- somut kanıt olmadan şirketi, yönetimi veya işlemi “manipülatif” olarak
  nitelendirmez;
- tanımsız veya keyfi 1-10 risk/güven skorları üretmez.

## 3. Girdi sözleşmesi

Her analiz girdisi mümkün olduğunda şu metadata ile taşınır:

- `document_id`, kaynak URL veya kaynak kimliği ve checksum;
- doküman türü ve yayın/dönem tarihi;
- şirket unvanı ve varsa BIST kodu;
- para birimi, ölçek (TL, bin TL, milyon TL) ve konsolidasyon kapsamı;
- bağımsız denetim durumu ve varsa revizyon ilişkisi;
- sayfa, tablo, dipnot veya paragraf konumu.

Eksik metadata, model tarafından tahmin edilmez; analizi etkiliyorsa eksik veri
olarak bildirilir.

## 4. Çıktı sözleşmesi

Yanıt, göreve uygun olan bölümleri kullanır:

1. **Sade özet:** Belgenin söylediğini açık Türkçeyle aktarır.
2. **Kanıtlanmış önemli noktalar:** Her iddiayı belge bölümü, dönem ve birimle
   ilişkilendirir.
3. **Finansal anlam:** Muhtemel etkileri kesin olgudan ayırır ve varsayımları
   görünür kılar.
4. **Dönem karşılaştırması:** Aynı tanım, para birimi, ölçek ve kapsam içindeki
   karşılaştırılabilir dönemleri kullanır.
5. **Eksik veri:** Yalnızca kararı veya yorumu maddi biçimde geliştirecek spesifik
   verileri ve neden gerektiğini belirtir.
6. **Belirsizlik/sınırlamalar:** Çelişki, okunamayan alan, muhasebe politikası
   değişimi veya kapsam farkını açıklar.

## 5. Fonksiyonel gereksinimler

### FR-01 — KAP açıklaması özeti

Sistem olayın ne olduğunu, tarafları, tarihi, tutarı/birimi, koşulları ve mevcut
durumu sade Türkçeyle özetlemelidir. Belirtilmeyen unsurlar “belirtilmemiş” olarak
kalmalıdır.

### FR-02 — Olası finansal anlam

Sistem olayın gelir, gider, varlık, yükümlülük, özkaynak ve nakit akışı üzerindeki
muhtemel kanallarını açıklamalı; gerçekleşmiş muhasebe etkisi ile geleceğe dönük
olasılığı birbirinden ayırmalıdır.

### FR-03 — Finansal tablo değişim analizi

Sistem gelir tablosu, bilanço ve nakit akış tablosundaki önemli değişimleri mutlak
tutar, karşılaştırılabilir oran ve dönem bazında saptamalıdır. Payda sıfırsa veya
dönemler karşılaştırılabilir değilse yanıltıcı yüzde üretmemelidir.

### FR-04 — Dönem karşılaştırılabilirliği

Sistem çeyrek/kümülatif dönem, enflasyon muhasebesi, para birimi, ölçek,
konsolidasyon ve yeniden sınıflandırma farklarını kontrol etmeden dönemsel sonuç
çıkarmamalıdır.

### FR-05 — Dipnot çıkarımı

Sistem en azından borç vade/faiz yapısı, teminat ve taahhütler, davalar,
karşılıklar, ilişkili taraf işlemleri, segmentler, kur riski, değer düşüklüğü,
süreklilik ve bilanço sonrası olaylar gibi maddi dipnotları taramalıdır.

### FR-06 — Veri yeterliliği

Sistem istenen analiz için girdinin yeterli olup olmadığını belirlemelidir. Eksik
ise genel “daha fazla veri” talebi yerine gerekli doküman/dönem/kalemi ve kullanım
amacını söylemelidir. Veri yeterliyse gereksiz takip sorusu sormamalıdır.

### FR-07 — Sayısal ve olgusal grounding

Her rakam kaynakla aynı değer, işaret, birim, para birimi ve dönemde olmalıdır.
Türetilen rakam açıkça “hesaplanan” diye etiketlenmeli ve yeniden üretilebilir
olmalıdır. Kaynaklar çelişirse öncelik veya seçim gerekçesi açıklanmalıdır.

### FR-08 — Rapor/piyasa ayrımı

Finansal doküman analizi ile fiyat, hacim, likidite ve endeks bağlamı ayrı
bölümlerde tutulmalıdır. Fiyat tepkisinin nedenselliği yalnızca zaman birlikteliği
üzerinden iddia edilmemelidir.

### FR-09 — Market context retrieval

BIST30/BIST50/BIST100 üyeliği, likidite, fiili dolaşım ve işlem hacmi model
ağırlıklarında kalıcı gerçek olarak kullanılmamalıdır. Bu alanlar kaynak ve
geçerlilik tarihi taşıyan retrieval kayıtlarından alınmalı, finansal rapor
doğruluğu göstergesi olarak kullanılmamalıdır.

### FR-10 — Somut yönetişim ve denetim sinyalleri

Sistem bağımsız denetim görüşünü, finansal tablo düzeltmelerini, ilişkili taraf
işlemlerini ve SPK/KAP açıklamalarını kaynakta yer aldığı kapsamda değerlendirmeli;
etiketleyici veya suçlayıcı sonucu kanıtsız genellememelidir.

### FR-11 — Kriter tabanlı göstergeler

İleride eklenen risk veya güven göstergeleri; girdi alanlarını, eşikleri, eksik
veri davranışını, hesap formülünü ve gerekçeyi göstermelidir. Tek başına modelin
öznel kanaatine dayanan puan yasaktır.

## 6. Kalite kapıları

- Numerical accuracy ve factual grounding için kritik hata toleransı sıfırdır.
- Hallüsinasyon içeren örnek, ortalama skor iyi olsa dahi ayrıca raporlanır.
- Eval split'i training ve model seçimi verisinden doküman ailesi bazında ayrıdır.
- Retrieval kullanılan testlerde bilgi kaynağı ve “as-of” tarihi kaydedilir.
- Her sürüm, `eval/rubrics/financial_document_analysis_v0.md` rubric'iyle
  karşılaştırılabilir bir rapor üretir.

## 7. Kabul ölçütleri — ilk ürün dilimi

İlk dikey dilim, sentetik olmayan ve lisans/izin durumu kayıtlı küçük bir eval
setinde KAP özeti ile iki dönemli finansal tablo karşılaştırmasını çalıştırmalı;
çıktıdaki her sayısal iddia kaynağa bağlanmalı ve eksik veri davranışı ayrı test
edilmelidir. Eşikler pilot annotation sonrasında, rubric değiştirilmeden önce
sürüm numarası artırılarak belirlenecektir.

