**IS TOTAAL NOG NIET WERKEND OF TE TESTEN!
NIET INSTALLEREN TOT NA DE ORDER !!**

# 🎨 Template Maker

Maak Home Assistant templates zonder YAML-trauma! Een visuele interface waar zelfs je oma mee overweg kan.

## ✨ Features

- 💡 Tel hoeveel lampen aan zijn
- 🌡️ Bereken gemiddelde temperatuur
- 🚪 Check of er iets open staat
- ⚡ Totaal stroomverbruik
- Geen YAML kennis nodig!

## 📦 Installatie

### Stap 1: Voeg de repository toe

Klik op deze knop in Home Assistant:

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https://github.com/iTzaRicoo/Automation-Maker-)

Of voeg handmatig toe:
1. Ga naar **Settings** → **Add-ons** → **Add-on Store**
2. Klik op het **⋮ menu** (3 puntjes rechtsboven) → **Repositories**
3. Voeg toe: `https://github.com/iTzaRicoo/Automation-Maker-`

### Stap 2: Installeer de add-on

1. Zoek **Template Maker** in de Add-on Store
2. Klik op **INSTALL**
3. Wacht tot installatie klaar is

### Stap 3: Configureer

1. Ga naar de **Configuration** tab
2. Maak een Long-Lived Access Token aan:
   - Ga naar je **Profile** → scroll naar beneden
   - Klik op **Create Token** onder "Long-Lived Access Tokens"
   - Geef het een naam (bijv. "Template Maker")
   - Kopieer het token (je ziet het maar 1 keer!)
3. Plak het token in het `supervisor_token` veld
4. Klik op **SAVE**

### Stap 4: Start de add-on

1. Ga terug naar de **Info** tab
2. Klik op **START**
3. Wacht tot de add-on gestart is (groen lampje)
4. Klik op **OPEN WEB UI**

### Stap 5: Activeer templates in Home Assistant

Voeg dit toe aan je `configuration.yaml`:

```yaml
template: !include_dir_merge_list include/templates/
```

**Herstart Home Assistant** om de templates te activeren!

## 🎯 Gebruik

1. Open de Web UI
2. Kies een template type
3. Selecteer je apparaten
4. Klik op "Template Maken!"
5. Herlaad Home Assistant
6. Je nieuwe sensor verschijnt automatisch!

## 🐛 Troubleshooting

### Addon is niet zichtbaar
- Check of de repository correct is toegevoegd
- Refresh de Add-on Store pagina
- Check de Supervisor logs

### "Supervisor Token ontbreekt" waarschuwing
- Maak een Long-Lived Access Token aan (zie Stap 3)
- Controleer of je het token correct hebt geplakt
- Herstart de add-on na het toevoegen

### Templates werken niet
- Controleer of `template: !include_dir_merge_list include/templates/` in configuration.yaml staat
- Herstart Home Assistant na het aanmaken van templates
- Check de Home Assistant logs voor fouten

## 📋 Changelog

### Version 1.0.0
- Eerste release
- 4 template types beschikbaar
- Visuele interface
- Automatische YAML generatie

## 🙋 Support

Heb je problemen? [Open een issue op GitHub](https://github.com/iTzaRicoo/Automation-Maker-/issues)

## 📄 Licentie

MIT License - Vrij te gebruiken!
