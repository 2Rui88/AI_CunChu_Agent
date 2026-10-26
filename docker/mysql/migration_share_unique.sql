-- Prevent duplicate public shares after Redis loss or concurrent requests.
-- Run after removing any pre-existing duplicate rows if the database already contains them.
ALTER TABLE share_file_list
    ADD UNIQUE KEY uq_share_md5_filename (md5, file_name);
