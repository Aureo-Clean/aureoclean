// Public settings for the AureoClean web pages. Nothing secret lives here.
window.AUREOCLEAN = {
  projectId: "aureoclean-b7c2b",
  // Optional: your Firebase *Web* API key (Project settings > General). Safe to publish,
  // but restrict it to this website in Google Cloud Console > Credentials.
  apiKey: "",

  // Restrooms shown on the report page. "code" is what the QR codes use (?r=CODE).
  // Edit this list to match your building (letters, numbers and dashes only in codes).
  locations: [
    { code: "GF-M", name: "Ground Floor · Male" },
    { code: "GF-F", name: "Ground Floor · Female" },
    { code: "2F-M", name: "2nd Floor · Male" },
    { code: "2F-F", name: "2nd Floor · Female" },
    { code: "3F-M", name: "3rd Floor · Male" },
    { code: "3F-F", name: "3rd Floor · Female" },
  ],
};
