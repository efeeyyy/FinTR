# Kurumsal aksiyon (corporate action) kaydı

## Kaynak ve kapsam

Bu proje kurumsal aksiyon (bedelsiz/bonus issue, rüçhan/rights issue, split,
ters split, other) olgularını **hiçbir zaman ağdan çekmez**. `kap.org.tr` ve
başka her ağ kaynağı bu araçlar için erişilemez durumda tutulur; bu bir
konfigürasyon değil, tasarım kararıdır. Olgu Efe tarafından birincil
kaynaktan okunup `scripts/register_corporate_action.py` ile girilir; bu araç
sadece Efe'nin girdiği veriyi doğrular ve provenance'ı (kaynak URL, yayın
tarihi, isteğe bağlı yerel dosya SHA-256, giriş zamanı) kaydeder.

## Şema: `fintr_corporate_actions_v1`

`configs/corporate_actions/<SEMBOL>.json`:

```json
{
  "schema": "fintr_corporate_actions_v1",
  "symbol": "SEMBOL",
  "actions": [
    {
      "action_id": "benzersiz-id",
      "type": "bonus_issue | rights_issue | split | reverse_split | other",
      "lot_multiplier": "6",
      "ex_date": "2026-05-04",
      "custody_effective_date": "2026-05-06",
      "source_url": "...",
      "source_published_date": "2026-05-01",
      "source_file_sha256": null,
      "entered_by": "user",
      "entered_at_utc": "2026-05-02T00:00:00Z",
      "verification": "user_asserted_from_primary_source",
      "notes": ""
    }
  ]
}
```

- `lot_multiplier`: eski lot başına yeni lot; **Decimal string** olarak
  tutulur (`"6"`), float değil, bir JSON sayısı olarak değil tırnaklı yazılır.
- `ex_date`: post-action birimlerde raporlanan ilk işlem tarihi; AKD ve fiyatı
  etkiler.
- `custody_effective_date`: post-action birimlerde raporlanan ilk takas
  tarihi. **Açıkça girilir, `ex_date`'ten türetilmez** -- takas genelde
  borsadan bir/birkaç gün geriden gelir.
- `verification`: yalnızca `"user_asserted_from_primary_source"` kabul edilir
  -- her kayıt Efe'nin birincil kaynaktan okuduğu bir iddiadır, bu projenin
  bağımsız olarak doğruladığı bir olgu değildir.

`src/fintr/data/corporate_actions.py` katı yükleyicisi reddeder: pozitif
olmayan `lot_multiplier`, `custody_effective_date < ex_date`, tekrarlayan
`action_id`/`ex_date`, ve **çakışan aksiyon pencereleri** (bir aksiyonun
`custody_effective_date`'i bir sonraki aksiyonun `ex_date`'inden sonraysa,
o aralıktaki çarpan belirsiz olur).

## Kayıt CLI'ı

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\register_corporate_action.py `
  --symbol SEMBOL --action-id benzersiz-id --type bonus_issue `
  --lot-multiplier 6 --ex-date 2026-05-04 --custody-effective-date 2026-05-06 `
  --source-url https://... --source-published-date 2026-05-01 `
  --source-file C:\path\to\indirilen-dosya.pdf
```

Her alan bir zorunlu argümandır (türetilen varsayılan yoktur).
`--source-file` verilirse dosyanın SHA-256'sı yerel olarak hesaplanır ve
kaydedilir; dosyanın kendisi **hiçbir zaman ağa yüklenmez veya okunmaz**.
Yazma atomiktir (geçici dosya + rename); mevcut, geçerli bir kayıt asla
kısmen üzerine yazılmaz -- doğrulama hatası (tekrarlayan `action_id`,
çakışma, geçersiz çarpan/tarih) dosyaya dokunmadan iptal eder. Başarılı
girişte eklenen kayıt onay için ekrana basılır.

## Düzeltilmiş görünüm (adjusted view)

`src/fintr/features/period_adjusted.py` (`FORMULA_VERSION =
"period_adjusted_v1"`), bir kaydı bir `PeriodCapture` üzerine uygulayıp
lot miktarlarını post-action birimlere **yeniden ifade eder** -- ham
bölümler asla değiştirilmez, `adjusted_view` tamamen ayrı bir rapor
bölümüdür:

- Günlük AKD `net_size` (ve `total_size`), `ex_date`'ten önceki tarihlerde
  çarpanla ölçeklenir; `total_cost` (fiyat) değişmez.
- Günlük takas `lot_count`, `custody_effective_date`'ten önceki tarihlerde
  ölçeklenir.
- Dönem AKD neti, düzeltilmiş günlük netlerin toplamı olarak **yeniden
  hesaplanır** -- sağlayıcının kendi dönem rakamı, dönem bir `ex_date`'i
  kapsıyorsa karışık birimlidir (`mixed_unit_period` bayrağıyla işaretlenir).
- Takas dönem farkı: dönem başlangıcı bir aksiyonun `custody_effective_date`'inden
  önceyse `first_value` yeniden ifade edilir ve fark, düzeltilmiş `first_value`
  ile ham `last_value`'dan yeniden hesaplanır.
- Kurum akış dizileri ve gecikme eşleşmesi, düzeltilmiş seriler üzerinde
  yeniden hesaplanır.
- **Tutarlılık kontrolü** (`check_corporate_action_consistency`): her
  aksiyonun `custody_effective_date`'indeki ham takas bakiyesi, `çarpan *
  önceki gün takas + ilgili işlem gününün AKD neti` tahminiyle karşılaştırılır
  (varsayılan gecikme `--consistency-lag 2`). Bu, **girilen aksiyonu verinin
  kendisine karşı test eder**; geçen bir kontrol aksiyonun doğru olduğunun
  bağımsız kanıtı değildir, sadece bu formülle uyumlu olduğunu gösterir.

`scripts/analyze_period.py --corporate-actions PATH` (tek kayıt modu
**yalnızca**; bir kayıt sembole özgüdür, `--all` modunda desteklenmez,
otomatik yükleme veya gizli varsayılan yoktur). Verilmezse `adjusted_view`
bölümü `{"status": "not_available"}` olur; kayıt sembolü capture'ın
sembolüyle uyuşmazsa `"symbol_mismatch"`, kayıtta hiç aksiyon yoksa
`"no_actions"` olur.

## Sürümlü rapor adlandırması

`--save`, rapor dosyasını `<capture-stem>.report.<period_features_version>
[.<price_join_version>][.adj-<kayıt_sha8>].json` olarak yazar (bkz.
`docs/features.md`). Bir kurumsal aksiyon kaydı kullanıldığında, dosya
adına kaydın SHA-256'sının ilk 8 hex karakteri eklenir; kaydın içeriği
değişirse (yeni aksiyon, düzeltme) yeni bir dosya adı üretilir ve eski rapor
asla üzerine yazılmaz.
