# NoxFlow Modular 2.7

## Final çalışma düzeni

Bu sürümde kaynak `nox` ile çalışma klonu `Nox_1` kesin olarak ayrılmıştır.

```text
C:\Program Files (x86)\Nox\bin\BignoxVMS\nox
    Kaynak şablon. Program tarafından açılmaz, değiştirilmez veya silinmez.

C:\Program Files (x86)\Nox\bin\BignoxVMS\Nox_1
    Çalışma klonu. Tüm Android, proxy, sertifika ve akış işlemleri burada yürür.
```

### Başlat düğmesi

1. Kaynak `nox` kaydı ve klasörü doğrulanır. Dışarıdan açık bırakıldıysa güvenlik için kapatılır.
2. Charles kapalıysa açılır; zaten açıksa mevcut Charles kullanılmaya devam edilir.
3. Seçici proxy geçidi açılır.
4. `Nox_1` NoxConsole kaydıyla ve klasörüyle geçerliyse aynen kullanılır.
5. `Nox_1` açıksa mevcut oturuma bağlanılır; kapalıysa `NoxConsole launch -name:Nox_1` ile açılır.
6. `Nox_1` yoksa Nox Multi-Drive/Assistant ile aynı Windows komutu kullanılır:

```bat
NoxConsole.exe copy -name:Nox_1 -from:nox
```

7. Yalnızca `Nox_1` açılır. Kaynak `nox` hiçbir zaman başlatılmaz.
8. ADB, 120 Hz yardımcı değerleri, seçici proxy, sertifika ve gömülü akış hazırlanır.

### Mevcut fakat kayıtsız Nox_1 klasörü

`Nox_1` klasörü diskte bulunuyor fakat NoxConsole listesinde kayıtlı değilse program bu klasörü
silmez. Önce tarihli bir yedeğe dönüştürür:

```text
Nox_1_yedek_YYYYMMDD_HHMMSS
```

Ardından NoxConsole üzerinden düzgün ve kayıtlı bir `Nox_1` klonu oluşturur.

### Klon kullanım sınırı

Panelde iki seçenek vardır:

- **Akış turu:** Örneğin 10. Akış on kez tamamlanınca mevcut çalışma klonu kapatılır ve yalnızca
  `Nox_1` NoxConsole üzerinden yenilenir.
- **Dakika:** Belirlenen süre tamamlandığında aynı yenileme yapılır.

Yenileme sırası:

```text
Nox_1 proxy temizliği
→ Nox_1 kapatma
→ ADB kapanışını bekleme
→ NoxConsole remove -name:Nox_1
→ NoxConsole copy -name:Nox_1 -from:nox
```

Kaynak `nox` bu işlemde yalnızca Nox'un kendi klonlama komutuna kaynak olur; çalıştırılmaz ve silinmez.

### Durdur ve pencerenin X düğmesi

Durdur veya X düğmesinde klon yenilemesi yapılmaz:

```text
Akışı kes
→ Nox_1 proxy ayarını temizle
→ Nox_1'i kapat
→ Nox_1 kaydını ve klasörünü koru
→ seçici geçidi kapat
→ runtime'ın açtığı Charles'ı kapat
```

Böylece bir sonraki Başlat işleminde mevcut `Nox_1` doğrudan kullanılır.

### Sertifika

Sertifika ilk kez başarıyla hazırlandığında kalıcı klona bir sürüm işareti yazılır. Aynı sertifika
aynı `Nox_1` üzerinde tekrar kurulmaz. Klon kullanım sınırında yenilenirse yeni klonda otomatik
olarak tekrar hazırlanır.

### Gömülü akış

```text
flows/Yeni_Akis_orijinal.noxflow.json
flows/gomulu_akis.noxflow.json
```

Runtime, sertifika adımları ayrılmış 12 adımlı `gomulu_akis.noxflow.json` sürümünü kullanır.

### Başlatma

```bat
start_runtime.bat
```

Konsol çıktısı için:

```bat
start_runtime_console.bat
```

Görsel adımlar için Pillow eksikse bir kez:

```bat
install_visual_support.bat
```


## 2.7 tam yaşam döngüsü

Başlat, tüm Nox görevlerini kapatır; eski `Nox_1` örneğini `NoxConsole remove` ile kaldırır; `NoxConsole copy -name:Nox_1 -from:nox` ile gerçek Multi-Drive klonunu oluşturur; klonu açar; Android boot sonrasında Charles, seçici proxy ve sertifikayı hazırlar; ardından gömülü uygulama akışını çalıştırır. Klasör kopyalama kullanılmaz.
