from datetime import date, datetime, timedelta
import io
import os
import tempfile
import warnings, os
warnings.filterwarnings('ignore')
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaFileUpload
from oauth2client.service_account import ServiceAccountCredentials
import streamlit as st
import pandas as pd
import gspread

def next_sunday():
    """
    Returns the date of the upcoming Sunday in 'YYYY-MM-DD' format.
    If today is already a Sunday, returns today's date.
    """
    today = date.today()
    # Monday=0 ... Sunday=6
    days_until_sunday = (6 - today.weekday()) % 7
    result = today + timedelta(days=days_until_sunday)
    return result.strftime("%Y-%m-%d")

 

# @title Common functions for Downloading and Uploading
SCOPES = ['https://www.googleapis.com/auth/drive']

def getGdriveService(credentialsFile, delegated_user=None):
    # Authenticates with Google Drive using a service account file
    # Pass delegated_user="someone@yourdomain.com" to impersonate a real user (needed if
    # uploading/downloading against a personal My Drive folder rather than a Shared Drive)
    GdriveCredentials = credentialsFile

    creds = service_account.Credentials.from_service_account_file(GdriveCredentials, scopes=SCOPES)

    if delegated_user:
        creds = creds.with_subject(delegated_user)

    return build('drive', 'v3', credentials=creds)

def getFilesList(parent_folder_id, service):
    # Retrieves ALL files/folders within a parent folder (paginated, Shared-Drive aware)
    file_list = []
    page_token = None
    while True:
        results = service.files().list(
            q=f"'{parent_folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora='allDrives'
        ).execute()
        file_list.extend(results.get('files', []))
        page_token = results.get('nextPageToken')
        if not page_token:
            break
    return file_list

def getSubfolderId(parent_folder_id, folder_name, service):
    # Looks up a named subfolder's ID within a parent folder
    for item in getFilesList(parent_folder_id, service):
        if item['name'] == folder_name:
            return item['id']
    return None


# @title Upload Files to Google Drive


def getOrCreateSubfolder(parent_folder_id, folder_name, service):
    folder_id = getSubfolderId(parent_folder_id, folder_name, service)
    if folder_id:
        return folder_id, True

    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_folder_id]
    }
    folder = service.files().create(
        body=file_metadata, fields='id', supportsAllDrives=True
    ).execute()
    return folder.get('id'), False

def deleteFilesInFolder(folder_id, service):
    for f in  getFilesList(folder_id, service) :
        service.files().delete(fileId=f['id'], supportsAllDrives=True).execute()

def getUniqueFileName(desired_name, existing_names):
    # Appends (1), (2), ... to desired_name until it no longer collides with existing_names
    if desired_name not in existing_names:
        return desired_name

    base, ext = os.path.splitext(desired_name)
    counter = 1
    while f"{base} ({counter}){ext}" in existing_names:
        counter += 1
    return f"{base} ({counter}){ext}"

def uploadFilesToGdrive(file_paths, folder_id, service, conflict_mode="overwrite"):
    """
    Uploads file_paths into folder_id.
    conflict_mode:
      - "overwrite": if a file with the same name exists, replace its content (same file ID)
      - "rename":    if a file with the same name exists, upload as "name (1).ext", "name (2).ext", etc.
    """
    assert conflict_mode in ("overwrite", "rename"), "conflict_mode must be 'overwrite' or 'rename'"

    existing_files = getFilesList(folder_id, service)          # [{'id':..., 'name':...}, ...]
    existing_by_name = {f['name']: f['id'] for f in existing_files}

    uploaded_ids = []
    for file_path in  file_paths :
        file_name = os.path.basename(file_path)
        media = MediaFileUpload(file_path, resumable=True)

        if file_name in existing_by_name:
            if conflict_mode == "overwrite":
                # Update existing file's content, keep same file ID
                file_id = existing_by_name[file_name]
                updated_file = service.files().update(
                    fileId=file_id, media_body=media, fields='id', supportsAllDrives=True
                ).execute()
                uploaded_ids.append(updated_file.get('id'))
                continue
            else:  # rename
                file_name = getUniqueFileName(file_name, existing_by_name.keys())

        file_metadata = {'name': file_name, 'parents': [folder_id]}
        uploaded_file = service.files().create(
            body=file_metadata, media_body=media, fields='id', supportsAllDrives=True
        ).execute()
        uploaded_ids.append(uploaded_file.get('id'))
        existing_by_name[file_name] = uploaded_file.get('id')  # track so later files in this batch don't collide too

    return uploaded_ids

def uploadFiles(file_paths, parent_folder_id, WSDate, service, delete_existing=True, conflict_mode="overwrite"):
    """
    Uploads file_paths into a subfolder named WSDate under parent_folder_id.
    - Creates the subfolder if it doesn't exist.
    - If it exists, deletes existing files first (delete_existing=True) —
      set delete_existing=False to skip that and just add files alongside what's there.
    """
    folder_id, exists = getOrCreateSubfolder(parent_folder_id, WSDate, service)

    if exists:
        st.write(f"Folder '{WSDate}' already exists.")
        if delete_existing:                     # <-- optional step
            st.write("Deleting existing files in the folder...")
            deleteFilesInFolder(folder_id, service)
    else:
        st.write(f"Created new folder '{WSDate}'.")

    st.write("Uploading files...")
    uploaded_ids = uploadFilesToGdrive([file_paths], folder_id, service)
    st.success(f"Uploaded {len(uploaded_ids)} file(s).")
    return uploaded_ids

def getSheet(sheet_id, sheet_name, credential_Upload):
  scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
  creds = ServiceAccountCredentials.from_json_keyfile_name(credential_Upload, scope)
  client = gspread.authorize(creds)

  workbook = client.open_by_key(sheet_id)
  values = workbook.worksheet(sheet_name).get_all_values()
  records = workbook.worksheet(sheet_name).get_all_records()
  # sheet_name = datetime.now().strftime("%b-%Y")

  # Read the downloaded XLSX file into a pandas DataFrame
  try:
      paymentSlugs = pd.DataFrame(values[1:], columns=values[0])
      return paymentSlugs, records, workbook
  except:
      return None
  
def save_upload(fileupload, fileType = None):
    temp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(temp_dir, fileupload.name)

    with open(tmp_path, "wb") as f:
        f.write(fileupload.getvalue())
    
    return tmp_path
        

st.set_page_config("📤 GDrive Bulk-Upload", layout="wide")
st.header("📤 GDrive Bulk-Upload", divider=True, text_alignment="center")

FolderMapping = {"Payment":"0AHGO663tIOm5Uk9PVA", "DirectUS":"0AHH0Svj1my00Uk9PVA", "Webinar Attendance":"0ADY0C0Grd3teUk9PVA"}

st.markdown("""
<style>
/* Widget label (the text passed into st.radio(...)) */
div[data-testid="stWidgetLabel"] p {
    font-size: 17px;
}
/* Text inside each dropdown option */
li[data-baseweb="menu-item"] div {
    font-size: 18px;
}

/* Individual option text */
div[data-testid="stSelectbox"] label div[data-testid="stMarkdownContainer"] p {
    font-size: 17px;
}

div[data-testid="stFileUploader"] label div[data-testid="stMarkdownContainer"] p {
    font-size: 17px;
}
</style>
""", unsafe_allow_html=True)


uploadOption  = st.selectbox(label="Select the upload file category", options=["Payment", "DirectUS", "Webinar Attendance"])

if uploadOption not in ["Webinar Attendance"]:
    WSDate = str(st.date_input("Select the Next Sunday date",value=next_sunday()))
    delPrevious = st.checkbox("Delete Existing?" )

st.divider()
credentialsGDriveFile = st.file_uploader("Upload the Gdrive Credentials",type=["json"])

if uploadOption in ["Webinar Attendance"]:
    credentialsFile = st.file_uploader("Upload the Credentials",type=["json"])

fileupload = st.file_uploader(f"Upload the {uploadOption} file(s)", type=["csv"], accept_multiple_files = True if uploadOption in ["Webinar Attendance"] else False)

if (fileupload and credentialsGDriveFile and WSDate and uploadOption not in ["Webinar Attendance"]) or \
    (uploadOption in ["Webinar Attendance"] and fileupload and credentialsGDriveFile and credentialsFile):     

    btn = st.button("Upload Files")

    if btn:
        with st.status("Processing...", expanded=True) as status:
            credentialsGDriveFile = save_upload(credentialsGDriveFile)
        
            service = getGdriveService(credentialsGDriveFile)

            if uploadOption in ["Webinar Attendance"]:
               

                credentialsFile = save_upload(credentialsFile)
                WebinarDetails, records, workbook = getSheet("1wUviIGWnfOeTTYW8dlnIspAi2G91mgMiP607i6PGncE", "WebinarDetails", credentialsFile)
                WebinarDetails = WebinarDetails[WebinarDetails["Cancelled"] != "Yes"]
                WebinarDetails["WebinarID"] = WebinarDetails["WebinarID"].str.replace(r"\W", "", regex=True)
                WebinarDetails.drop_duplicates(subset=WebinarDetails.columns, inplace=True)
                WebinarDetails["Date"] = pd.to_datetime(WebinarDetails["Date"], format="%d-%m-%Y", exact=True).dt.date
            
                RawName = [ save_upload(file)  for file in fileupload]
                UploadedFileName = {os.path.basename(file) : file for file in RawName}

                WebinarDetails["BatchName"] = WebinarDetails["BatchName"].str.strip().str.upper()

                WebinarList = WebinarDetails[WebinarDetails["FileName"].isin(UploadedFileName.keys())][["BatchName","FileName"]]\
                                        .groupby("BatchName")["FileName"].agg(list).reset_index()
                
                WebinarDict = WebinarList.set_index("BatchName").to_dict()["FileName"]

                for batch in WebinarDict:
                    for filename in WebinarDict[batch]:
                        #st.write(batch, UploadedFileName[filename])
                        uploadFiles(UploadedFileName[filename] , FolderMapping[uploadOption] , batch, service,  False, "overwrite")

                missing_data = pd.DataFrame(data={"Mapping Issue" : [i for i in UploadedFileName.keys() if i not in WebinarDetails["FileName"].unique()]})
                st.dataframe(missing_data, width="stretch", hide_index=True)
                
            else:
                uploadFiles(save_upload(fileupload) , FolderMapping[uploadOption] , WSDate, service, True if delPrevious else False, "overwrite")
            status.update(state="complete", expanded=False )


        st.success("File uploaded.")
    
elif uploadOption not in ["Webinar Attendance"]:
    with st.status("Links", expanded=False):
            col1, col2, col3, col4, col5  = st.columns(5, vertical_alignment = "center",  width="stretch") 
         
            with col1:
               st.link_button("Open 10xStats", "https://10xstats.com/", width  = "stretch")
            with col2:
               st.link_button("Open DirectUS", "https://directus-production-62b2.up.railway.app/admin/users/", width  = "stretch") 
            with col3:
               st.link_button("Open MEGA Main", "https://megamain.streamlit.app/", width  = "stretch") 
            with col4:
               st.link_button("Open MEGA Exotic", "https://megaexotic.streamlit.app/", width  = "stretch") 
            with col5:
               st.link_button("Open MEGA AC", "https://megaac.streamlit.app/", width  = "stretch")
