// Sensor board example for the Explore Rwanda website: an ESP32 with a DHT22 temperature and humidity sensor.
//
// Every 5 minutes it reads the sensor and sends the values to the website. Set the four values below, then
// upload the sketch. The token is shown once on the website (Sensor monitoring > Devices > Add a device).
//
// Libraries (install from the Arduino Library Manager): "DHT sensor library" by Adafruit, and the
// ESP32 board package (the WiFi, WiFiClientSecure and HTTPClient libraries come with it).
//
// Security note: client.setInsecure() below skips checking the server's certificate, so the connection is
// encrypted but the server is not verified. For a permanent installation, load the website's root certificate
// with client.setCACert() instead.

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <DHT.h>

const char* WIFI_SSID = "your-wifi-name";
const char* WIFI_PASSWORD = "your-wifi-password";
const char* API_URL = "https://tour-travels-2.onrender.com/api/iot/readings/";
const char* DEVICE_TOKEN = "paste-the-token-shown-once-by-the-website";

#define DHT_PIN 4
#define DHT_TYPE DHT22
const unsigned long INTERVAL_MS = 5UL * 60UL * 1000UL;

DHT dht(DHT_PIN, DHT_TYPE);

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 30000) {
    delay(500);
  }
  Serial.println(WiFi.status() == WL_CONNECTED ? "WiFi connected" : "WiFi not connected yet");
}

bool sendReadings(float temperature, float humidity) {
  WiFiClientSecure client;
  client.setInsecure();  // see the security note at the top

  HTTPClient http;
  if (!http.begin(client, API_URL)) {
    Serial.println("Could not open the connection to the website");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);

  String body = String("{\"readings\":{\"temperature\":") + String(temperature, 1) +
                ",\"humidity\":" + String(humidity, 1) + "}}";
  int code = http.POST(body);
  String reply = http.getString();
  http.end();

  // 201 = stored. 401 = wrong or revoked token. 429 = too many readings. 400 = the reply says why.
  Serial.printf("Website answered %d: %s\n", code, reply.c_str());
  return code == 201;
}

void setup() {
  Serial.begin(115200);
  dht.begin();
  connectWifi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWifi();
  }
  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();
  if (isnan(humidity) || isnan(temperature)) {
    Serial.println("The DHT22 sensor did not answer; nothing sent this time");
  } else if (WiFi.status() == WL_CONNECTED) {
    sendReadings(temperature, humidity);
  }
  delay(INTERVAL_MS);
}
