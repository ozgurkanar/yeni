# NoxFlow Proje Devir Belgesi — v2.8

## Amaç

Tek Başlat düğmesiyle sıfırdan temiz bir Nox çalışma klonu oluşturmak, ağ ve sertifika hazırlığını yapmak ve kayıtlı Android otomasyon akışını çalıştırmak.

## Beklenen Başlat sırası

1. Bütün Nox görevlerini sonlandır.
2. `Nox_1` varsa NoxConsole ile kaldır.
3. `NoxConsole copy -name:Nox_1 -from:nox` ile gerçek Nox klonu oluştur.
4. Klonun NoxConsole kaydını ve disk durumunu bekle.
5. `NoxConsole launch -name:Nox_1` ile aç.
6. ADB ve Android boot tamamlanmasını bekle.
7. Charles ve seçici proxy geçidini başlat.
8. Sertifikayı yeni klona yükle/kur.
9. Gömülü akışı çalıştır.
10. Tur sonunda Nox görevlerini kapat; devam edilecekse yeni klon döngüsünü tekrarla.

## Kritik ayrım

Klon üretmek, `BignoxVMS/nox` klasörünü Python ile kopyalamak değildir. Klon yalnız Nox'un kendi NoxConsole `copy` komutuyla oluşturulmalıdır. Böylece Nox Assistant/Multi-Drive'ın kayıt, UUID ve sanal makine yapılandırma davranışı kullanılır.

## Ana dosyalar

- `runtime_panel.py`: kullanıcı arayüzü
- `noxflow/orchestrator.py`: bütün yaşam döngüsünün sırası
- `noxflow/nox.py`: NoxConsole, süreç kapatma ve klon işlemleri
- `noxflow/adb.py`: ADB ve Android hazır olma kontrolleri
- `noxflow/proxy.py`: Charles ve seçici proxy
- `noxflow/certificate.py`: sertifika hazırlığı
- `noxflow/flow.py`: NoxFlow JSON adım motoru
- `config/runtime.json`: yollar ve çalışma tercihleri
- `flows/gomulu_akis.noxflow.json`: runtime akışı
- `.github/copilot-instructions.md`: Copilot için bağlayıcı talimatlar

## Bilinen doğrulama ihtiyacı

Gerçek Nox kurulumunda NoxConsole komut söz dizimi ve dönen liste biçimi kuruluma göre test edilmelidir. Hatalarda komut, dönüş kodu, stdout ve stderr Günlük sekmesine yazılmalıdır.
