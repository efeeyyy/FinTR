# FinTR-LLM agent rules

Bu kurallar repository'de çalışan tüm kodlama ajanları için geçerlidir.

## Değişiklik disiplini

- Değişiklikleri küçük, tek amaçlı, gözden geçirilebilir ve test edilebilir tut.
- İlgili testi veya doğrulama komutunu değişiklikle birlikte ekle ve çalıştır.
- Kullanıcının mevcut değişikliklerini silme veya ilgisiz dosyaları yeniden
  biçimlendirme.
- Önemli mimari değişikliklerden önce problem, alternatifler, karar ve sonuçları
  kısa bir ADR ya da `docs/architecture.md` güncellemesiyle gerekçelendir.

## Finansal doğruluk ve kaynak gösterme

- Kaynak dokümanda bulunmayan finansal rakam, tarih, oran, olay veya iddia üretme.
- Hesaplanan değerleri kaynakta verilen değerlerden ayır; formülü, birimi, dönemi
  ve kullanılan girdileri kaydet.
- Bir bilgi doğrulanamıyorsa bunu açıkça belirsiz veya eksik olarak işaretle.
- Finansal rapor içeriği ile piyasa fiyat davranışını ayrı veri alanları ve ayrı
  çıkarımlar olarak ele al.

## Veri bütünlüğü

- Evaluation setini training, validation, tokenizer eğitimi, örnek seçimi veya
  prompt optimizasyonu verisine karıştırma.
- Doküman ailesi, şirket, rapor dönemi ve olay bazında sızıntıyı engelleyen split
  kuralları kullan; yalnızca satır bazlı rastgele split yapma.
- Aynı dokümanın kopyalarını, revizyonlarını ve türetilmiş parçalarını split'ler
  arasında tespit et ve tek grupta tut.
- Eval yanıtlarını training örneği, retrieval corpus'u veya model seçim girdisi
  olarak kullanma.
- Her veri artefact'ında kaynak, erişim zamanı, lisans/izin, checksum ve dönüşüm
  soyunu izlenebilir tut.

## Tekrar üretilebilirlik

- Deneylerde kod commit'i, config, veri manifesti/sürümü, seed, ortam ve donanım
  bilgisini kaydet.
- Rastlantısallığı kontrol et; deterministik olmayan işlemleri ve beklenen
  etkilerini belgele.
- El ile değiştirilen gizli varsayılanlardan kaçın; parametreleri config'e koy.
- Sonuç iddiasını tekrar çalıştırılabilir komut ve üretilen metric artefact'ıyla
  destekle.

## Güvenlik sınırları

- Modeli yatırım tavsiyesi veya doğrudan al/sat kararı üretmeye yönlendirme.
- İstisna (Efe kararı, 2026-10-02): deterministik bildirim botu (`fintr.notify`), yalnız Efe'nin kişisel kullanımı için, dondurulmuş ön kayıtlı kuralın `predicted_direction` alanını "beklenen yön: yukarı (al yönlü)" / "beklenen yön: aşağı (sat yönlü)" etiketiyle gösterebilir. Etiket her mesajda doğrulanmamış hipotez uyarısıyla verilir; kural, eşik veya spec değiştirmez; getiri, puan, hedef fiyat, stop seviyesi veya pozisyon büyüklüğü eklemez. LLM bileşenleri bu istisnaya dahil değildir. Holdout testi ve K4 (2027-03-31'e kadar ara performans değerlendirmesi yok) aynen geçerlidir. Ayrıntı: `docs/prereg/DECISIONS.md`.
- İstisna (Efe kararı, 2026-10-02): günlük analiz notu adımı (Claude Opus 5.5) etiketli gözlemler için yalnız kanıt değerlendirmesi (`destekliyor`/`zayıflatıyor`/`belirsiz`) üretebilir; al/sat emri, hedef, stop, pozisyon, getiri tahmini veya keyfi puan üretmez; çıktıdaki her rakam girdide bulunmak zorundadır (kodla denetlenir); not başarısız olursa deterministik bildirim yine gönderilir. Ham verinin Anthropic'e gönderimi Efe'nin kararıdır; lisans kapsamı doğrulanmadı.
- Kanıt olmadan şirketleri veya işlemleri manipülatif olarak nitelendirme.
- Keyfi 1-10 güven/risk skoru ekleme; her göstergeyi açık, denetlenebilir
  kriterlere bağla.

