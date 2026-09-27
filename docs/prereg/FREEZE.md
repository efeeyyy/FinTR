# Dondurma kaydı

Dondurma zamanı (UTC): 2026-09-27T07:42:58.062765Z. Onay: Efe, 2026-09-27 (DECISIONS.md). Dondurma, geliştirme
verisinde hiçbir getiri okunmadan yapıldı; yalnız veri kalitesi (lot/hacim ölçeği) kontrol edildi.
Birincil aile H1+H2 (Holm m=2); H3, H4 ikincil/tanımlayıcı.

| Dosya | spec_id | SHA-256 |
|---|---|---|
| `h1_close_premium_vs_vwap_proxy_v1.json` | `prereg_bist30_h1_close_premium_vs_vwap_proxy_v1` | `15766a1684f595fa7d0a749feca9431879044dc5243f13b39966a4879307174a` |
| `h2_close_discount_vs_vwap_proxy_v1.json` | `prereg_bist30_h2_close_discount_vs_vwap_proxy_v1` | `21a620295b27b4cc2cf207f312faa97dd47ad788211a3bf08d58ea37bbf9b8f6` |
| `h3_concentrated_flow_close_premium_v1.json` | `prereg_bist30_h3_concentrated_flow_close_premium_v1` | `f0d4a5978bae62bd2a74e63b6f4d3e1e6683d2b78167baf944a9195f92645762` |
| `h4_concentrated_flow_close_discount_v1.json` | `prereg_bist30_h4_concentrated_flow_close_discount_v1` | `05786bb9a75e2ae3a556b24df439502c478da2fa815776f7db4e4ff9b4cb629f` |

Bu dosyalar bundan sonra değiştirilmez; değişiklik yeni sürüm (v2) ve yeni ön kayıt gerektirir.

## Kanonik spec hash'i (çalışma çıktılarındaki `spec_sha256`)

Yukarıdaki SHA-256 dosyanın ham baytlarıdır. Çalışma çıktıları ise
`fintr.research.study_spec.load_study_spec` tarafından hesaplanan kanonik JSON
hash'ini kaydeder; bir çıktının hangi dondurulmuş spec ile üretildiğini doğrulamak için
bu sütun kullanılır.

| Dosya | kanonik SHA-256 |
|---|---|
| `h1_close_premium_vs_vwap_proxy_v1.json` | `0a93cb8fb6c512db4e727a5188cb720f9ec832d6dbd64ef33215e38ca546e7c2` |
| `h2_close_discount_vs_vwap_proxy_v1.json` | `2adc380cf25f648afba622ed1e0eea5af5f442abecf0b8aa11d9e8d999acf29c` |
| `h3_concentrated_flow_close_premium_v1.json` | `5f6e931205a54dc7ffdaea9f9e78744be3c6082cd5bdd5e0821d20aa2258941d` |
| `h4_concentrated_flow_close_discount_v1.json` | `f66d7c90e7cac34921da76a8d5111091257199ffb09e5b2aafce388538209b4b` |
