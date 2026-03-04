from google_auth_oauthlib.flow import InstalledAppFlow

# This permission allows the bot to read/write files it creates
SCOPES = ['https://www.googleapis.com/auth/drive.file']

def main():
    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
    # This will open your web browser
    creds = flow.run_local_server(port=0)

    # Save the credentials for the server to use
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    print("✅ Success! token.json has been created.")

if __name__ == '__main__':
    main()