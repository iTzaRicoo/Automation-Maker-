\# Automation Maker (Home Assistant Add-on)



Automation Maker is een (bijna schandalig) simpele manier om Home Assistant automations te maken, bewerken en testen, zonder dat je eerst een studie “YAML \& Verdriet” hoeft af te ronden.



Je kiest:

\- \*\*WANNEER\*\* iets moet gebeuren  

\- \*\*DAN\*\* wat er moet gebeuren  

\- drukt op \*\*Test\*\*  

\- en krijgt een uitleg die zelfs je buurman begrijpt die denkt dat “Home Assistant” een nieuwe zorgverzekering is.



\## Waarom bestaat dit?



Omdat Home Assistant fantastisch is, maar soms voelt het maken van automations alsof je een IKEA-kast probeert te bouwen met alleen een cryptische tekening en één schroef over.



Automation Maker doet hetzelfde, maar dan met:

\- duidelijke keuzes

\- een vriendelijke interface

\- testresultaten die stap voor stap “afspelen”

\- en een knop die letterlijk zegt: \*\*Leg uit alsof ik 5 ben\*\*



\## Features



\- Automations maken via een simpele UI (WANNEER → DAN)

\- Bestaande automations bekijken en bewerken

\- \*\*Test-run\*\* met animaties (stap voor stap, lekker duidelijk)

\- \*\*Beginner-modus\*\*: verstopt woorden als `service` en `entity\_id` alsof ze nooit bestaan hebben

\- \*\*ELI5-modus\*\*: “Leg uit alsof ik 5 ben” maakt de uitleg nog simpeler

\- Automations worden netjes als YAML opgeslagen in je gekozen map

\- Automations reload na opslaan (geen “waarom werkt het niet” rondje door de UI)



\## Hoe ziet “Test” eruit?



In plaats van:  

> “Calling service light.turn\_on on entity\_id light.keuken HTTP 200”



Krijg je:  

> “We geven Home Assistant de opdracht.”  

> “Home Assistant zegt: gelukt.”



Dat is het hele punt.



\## Installatie (Local add-on)





Binnenkort beschikbaar.



---



\## Gebruik



1\. Geef je automation een naam (iets als “Lamp aan in de avond”, niet “test123\_final\_final2”)

2\. Kies \*\*WANNEER\*\*

3\. Kies \*\*DAN\*\*

4\. Klik \*\*Opslaan\*\*  

&nbsp;  of  

&nbsp;  Klik \*\*Test\*\* als je eerst wil zien of Home Assistant überhaupt zin heeft vandaag



\### ELI5 knop

In het testpanel zit een knop:



\*\*“Leg uit alsof ik 5 ben”\*\*



Als die aan staat:

\- worden teksten nog simpeler

\- verdwijnen technische details

\- en voelt alles alsof het gemaakt is voor normale mensen (rare doelgroep, maar toch)



\## Wat wordt opgeslagen?



Automations worden als `.yaml` opgeslagen in:



`/config/include/automations/`



(tenzij je het in de add-on opties anders instelt)



\## Roadmap / To do



\- UI polish en nog meer “oh, dit snap ik” momentjes

\- Automation sanity check: “Deze automation kan zichzelf oneindig triggeren. Dat is een slecht idee.”

\- Conflict-detectie: “Je hebt al een automation die deze lamp om 19:00 uitzet… botsing?”

\- ‘Are you sure?’ bij gevaarlijke acties: (Alles uit, verwarming uit bij -10, etc.)

\- Zoeken in normaal Nederlands, Typ: “lamp avond” → juiste automation verschijnt.

\- Easter eggs

\- Vele andere ideeen, die vast op kunnen komen.



\## Bekende bijwerkingen



\- Je gaat ineens veel meer automations maken “omdat het nu toch makkelijk is”

\- Je vrienden gaan vragen of je “even hun Home Assistant wil fixen”

\- Je ontdekt dat je lampen al die tijd prima waren, jij niet



\## Contributing



PR’s zijn welkom.  

Bugs ook, maar graag met:

\- wat je deed

\- wat je verwachtte

\- wat er gebeurde

\- en of je Home Assistant je daarna uitlachte



\## Disclaimer



Deze add-on probeert het leven makkelijker te maken.  

Home Assistant blijft Home Assistant.  

Soms wint de robot.



\## License



Kies een license die bij je past.  

Ik ben maar een README, geen advocaat.



