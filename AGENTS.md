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
- Kanıt olmadan şirketleri veya işlemleri manipülatif olarak nitelendirme.
- Keyfi 1-10 güven/risk skoru ekleme; her göstergeyi açık, denetlenebilir
  kriterlere bağla.

